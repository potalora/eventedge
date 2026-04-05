"""Core generation management for multi-version autoresearch.

Each generation is a frozen snapshot of the codebase at a specific commit,
running in its own git worktree with isolated state. This allows comparing
trading performance across code versions side-by-side.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_RUN_HISTORY = 100


@dataclass
class GenerationInfo:
    """Metadata for a single generation (frozen code snapshot)."""

    gen_id: str               # "gen_001", "gen_002", ...
    git_commit: str           # Full SHA
    git_branch: str           # Branch name at creation time
    worktree_path: str        # Absolute path to .worktrees/gen_NNN
    state_dir: str            # Absolute path to data/generations/gen_NNN
    created_at: str           # ISO timestamp
    status: str               # "active", "paused", "retired"
    description: str          # User-provided description
    run_history: list[dict] = field(default_factory=list)


class GenerationManager:
    """Manage multiple frozen code generations via git worktrees."""

    def __init__(
        self,
        repo_root: str,
        generations_dir: str = "data/generations",
    ):
        self._repo_root = Path(repo_root).resolve()
        self._generations_dir = (self._repo_root / generations_dir).resolve()
        self._worktrees_dir = (self._repo_root / ".worktrees").resolve()
        self._manifest_path = self._generations_dir / "manifest.json"

        # Use the venv python WITHOUT resolving symlinks — resolve() follows
        # the symlink chain to the base interpreter, which loses the venv
        # context (site-packages, installed packages like openbb).
        repo_venv = self._repo_root / ".venv" / "bin" / "python"
        if repo_venv.exists():
            self._venv_python = repo_venv.absolute()
        else:
            self._venv_python = Path(sys.executable).absolute()

        # Ensure directories exist
        self._generations_dir.mkdir(parents=True, exist_ok=True)
        self._worktrees_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_generation(self, description: str) -> GenerationInfo:
        """Create a new generation from current HEAD.

        1. Capture current commit and branch.
        2. Allocate next gen_id.
        3. Create a detached git worktree at the commit.
        4. Create an isolated state directory.
        5. Persist to manifest.
        """
        # 0. Warn about uncommitted changes (worktree won't include them)
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self._repo_root,
            capture_output=True, text=True,
        )
        if status_result.stdout.strip():
            logger.warning(
                "Working directory has uncommitted changes. "
                "The generation worktree will only contain committed code. "
                "Commit your changes first for a clean snapshot."
            )

        # 1. Get commit and branch
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # 2. Allocate gen_id
        gen_id = self._next_gen_id()

        # 3. Create worktree
        worktree_path = self._worktrees_dir / gen_id
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), commit, "--detach"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        logger.info("Created worktree %s at %s", gen_id, commit[:12])

        # 4. Create state directory
        state_dir = self._generations_dir / gen_id
        state_dir.mkdir(parents=True, exist_ok=True)

        # 5. Build info and persist
        info = GenerationInfo(
            gen_id=gen_id,
            git_commit=commit,
            git_branch=branch,
            worktree_path=str(worktree_path),
            state_dir=str(state_dir),
            created_at=datetime.now().isoformat(),
            status="active",
            description=description,
            run_history=[],
        )

        manifest = self._load_manifest()
        manifest["generations"].append(asdict(info))
        self._save_manifest(manifest)

        logger.info(
            "Started generation %s: branch=%s commit=%s",
            gen_id, branch, commit[:12],
        )
        return info

    def run_daily(self, trading_date: str | None = None) -> dict[str, dict]:
        """Run daily trading for all active generations.

        Each generation runs in its own worktree via subprocess, with
        AUTORESEARCH_STATE_DIR and PYTHONPATH set for isolation.

        Returns:
            {gen_id: {"success": bool, "elapsed_s": float, "error"?: str}}
        """
        if not trading_date:
            trading_date = datetime.now().strftime("%Y-%m-%d")

        results: dict[str, dict] = {}
        manifest = self._load_manifest()

        for gen_data in manifest["generations"]:
            if gen_data["status"] != "active":
                continue

            gen_id = gen_data["gen_id"]
            logger.info("Running daily for %s (date=%s)", gen_id, trading_date)

            result = self._run_cohorts_subprocess(
                gen_data,
                ["--date", trading_date],
            )
            results[gen_id] = result

            # Record in run_history
            gen_data["run_history"].append({
                "date": trading_date,
                "action": "daily",
                "success": result["success"],
                "elapsed_s": result["elapsed_s"],
                **({"error": result["error"]} if "error" in result else {}),
            })
            # Cap history
            gen_data["run_history"] = gen_data["run_history"][-_MAX_RUN_HISTORY:]

        self._save_manifest(manifest)
        return results

    def run_learning(self) -> dict[str, dict]:
        """Run the learning loop for all active generations.

        Returns:
            {gen_id: {"success": bool, "elapsed_s": float, "error"?: str}}
        """
        results: dict[str, dict] = {}
        manifest = self._load_manifest()

        for gen_data in manifest["generations"]:
            if gen_data["status"] != "active":
                continue

            gen_id = gen_data["gen_id"]
            logger.info("Running learning for %s", gen_id)

            result = self._run_cohorts_subprocess(
                gen_data,
                ["--learning"],
            )
            results[gen_id] = result

            # Record in run_history
            gen_data["run_history"].append({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "action": "learning",
                "success": result["success"],
                "elapsed_s": result["elapsed_s"],
                **({"error": result["error"]} if "error" in result else {}),
            })
            gen_data["run_history"] = gen_data["run_history"][-_MAX_RUN_HISTORY:]

        self._save_manifest(manifest)
        return results

    def pause_generation(self, gen_id: str) -> None:
        """Set a generation's status to 'paused'."""
        manifest = self._load_manifest()
        gen_data = self._find_gen(manifest, gen_id)
        if gen_data is None:
            raise ValueError(f"Generation {gen_id} not found")
        if gen_data["status"] == "retired":
            raise ValueError(f"Cannot pause retired generation {gen_id}")
        gen_data["status"] = "paused"
        self._save_manifest(manifest)
        logger.info("Paused generation %s", gen_id)

    def resume_generation(self, gen_id: str) -> None:
        """Resume a paused generation back to 'active'."""
        manifest = self._load_manifest()
        gen_data = self._find_gen(manifest, gen_id)
        if gen_data is None:
            raise ValueError(f"Generation {gen_id} not found")
        if gen_data["status"] != "paused":
            raise ValueError(
                f"Can only resume paused generations, {gen_id} is {gen_data['status']}"
            )
        gen_data["status"] = "active"
        self._save_manifest(manifest)
        logger.info("Resumed generation %s", gen_id)

    def retire_generation(
        self,
        gen_id: str,
        delete_worktree: bool = True,
    ) -> None:
        """Retire a generation. Optionally remove its git worktree.

        The state directory is preserved for historical comparison.
        """
        manifest = self._load_manifest()
        gen_data = self._find_gen(manifest, gen_id)
        if gen_data is None:
            raise ValueError(f"Generation {gen_id} not found")

        gen_data["status"] = "retired"

        if delete_worktree:
            worktree_path = gen_data["worktree_path"]
            if Path(worktree_path).exists():
                try:
                    subprocess.run(
                        ["git", "worktree", "remove", worktree_path, "--force"],
                        cwd=self._repo_root,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    logger.info("Removed worktree for %s", gen_id)
                except subprocess.CalledProcessError as e:
                    logger.warning(
                        "Failed to remove worktree for %s: %s",
                        gen_id, e.stderr.strip(),
                    )

        self._save_manifest(manifest)
        logger.info("Retired generation %s", gen_id)

    def list_generations(self) -> list[GenerationInfo]:
        """Return all generations from manifest."""
        manifest = self._load_manifest()
        return [
            GenerationInfo(**gen_data)
            for gen_data in manifest["generations"]
        ]

    def get_generation(self, gen_id: str) -> GenerationInfo | None:
        """Look up a single generation by ID."""
        manifest = self._load_manifest()
        gen_data = self._find_gen(manifest, gen_id)
        if gen_data is None:
            return None
        return GenerationInfo(**gen_data)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _run_cohorts_subprocess(
        self,
        gen_data: dict,
        extra_args: list[str],
    ) -> dict:
        """Run scripts/run_cohorts.py in a generation's worktree.

        Sets AUTORESEARCH_STATE_DIR and PYTHONPATH for isolation.
        Returns {"success": bool, "elapsed_s": float, "error"?: str}.
        """
        env = os.environ.copy()
        env["AUTORESEARCH_STATE_DIR"] = str(Path(gen_data["state_dir"]).resolve())
        env["PYTHONPATH"] = str(Path(gen_data["worktree_path"]).resolve())

        cmd = [
            str(self._venv_python),
            "scripts/run_cohorts.py",
            *extra_args,
        ]

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=gen_data["worktree_path"],
                env=env,
                capture_output=True,
                text=True,
                timeout=2700,
            )
            elapsed = time.monotonic() - start

            if proc.returncode != 0:
                error_msg = (proc.stderr or proc.stdout or "").strip()
                # Truncate long error output
                if len(error_msg) > 2000:
                    error_msg = error_msg[:2000] + "...(truncated)"
                logger.error(
                    "Generation %s failed (rc=%d): %s",
                    gen_data["gen_id"], proc.returncode, error_msg[:200],
                )
                return {
                    "success": False,
                    "elapsed_s": round(elapsed, 2),
                    "error": error_msg,
                }

            logger.info(
                "Generation %s completed in %.1fs",
                gen_data["gen_id"], elapsed,
            )
            return {"success": True, "elapsed_s": round(elapsed, 2)}

        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            logger.error("Generation %s timed out after %.0fs", gen_data["gen_id"], elapsed)
            return {
                "success": False,
                "elapsed_s": round(elapsed, 2),
                "error": "Timed out after 600s",
            }
        except Exception as e:
            elapsed = time.monotonic() - start
            logger.error("Generation %s error: %s", gen_data["gen_id"], e)
            return {
                "success": False,
                "elapsed_s": round(elapsed, 2),
                "error": str(e),
            }

    def _next_gen_id(self) -> str:
        """Return the next sequential gen_id like 'gen_001', 'gen_002'."""
        manifest = self._load_manifest()
        if not manifest["generations"]:
            return "gen_001"

        # Parse existing IDs to find the max
        max_num = 0
        for gen_data in manifest["generations"]:
            try:
                num = int(gen_data["gen_id"].split("_")[1])
                max_num = max(max_num, num)
            except (IndexError, ValueError):
                continue
        return f"gen_{max_num + 1:03d}"

    def _find_gen(self, manifest: dict, gen_id: str) -> dict | None:
        """Find a generation dict in the manifest by ID."""
        for gen_data in manifest["generations"]:
            if gen_data["gen_id"] == gen_id:
                return gen_data
        return None

    def _load_manifest(self) -> dict:
        """Load manifest.json. Returns empty structure if not found."""
        if not self._manifest_path.exists():
            return {"generations": []}
        try:
            with open(self._manifest_path) as f:
                data = json.load(f)
            if "generations" not in data:
                data["generations"] = []
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load manifest: %s", e)
            return {"generations": []}

    def _save_manifest(self, data: dict) -> None:
        """Atomic write of manifest.json."""
        self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self._manifest_path.parent,
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, default=str)
                f.write("\n")
            os.replace(tmp, self._manifest_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
