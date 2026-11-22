"""Unit tests for Wave 0 foundational contracts: GenerationConfig, _io, and GenerationDB."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess

import pytest

from tradingagents.default_config import DEFAULT_CONFIG, DEFAULT_GENERATION_CONFIG
from tradingagents.strategies.config import (
    ACTIVE_STRATEGIES,
    ConfigValidationError,
    GenerationConfig,
    PaperLedgerConfig,
    PortfolioPolicyConfig,
    RiskDisciplineConfig,
    ShortSellingConfig,
    load_generation_config,
)
from tradingagents.strategies.orchestration._io import (
    CANONICAL_COHORTS,
    COHORT_HORIZONS,
    COHORT_SIZES,
    SOURCE_TAXONOMY,
    atomic_write_json,
    atomic_write_text,
    load_json_strict,
    open_sqlite,
    snapshot_sqlite,
    verify_git_worktree,
)
from tradingagents.strategies.state.generation_db import (
    ALL_TABLES,
    COHORT_TABLES,
    GENERATION_DB_FILENAME,
    SCHEMA_VERSION,
    SHARED_TABLES,
    GenerationDB,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
GEN003_PROFILE = REPO_ROOT / "config" / "profiles" / "gen003-clean.json"


# ---------------------------------------------------------------------------
# 1. GenerationConfig + gen003-clean.json + DEFAULT_CONFIG
# ---------------------------------------------------------------------------


class TestGenerationConfig:
    def test_gen003_clean_profile_matches_exact_contract(self) -> None:
        cfg = load_generation_config(GEN003_PROFILE)
        assert cfg.enabled_strategies == (
            "earnings_call",
            "supply_chain",
            "govt_contracts",
            "weather_ag",
            "commodity_macro",
        )
        assert cfg.llm_billing_mode == "chatgpt_plan"
        assert cfg.thesis_model == "gpt-6.2-sol"
        assert cfg.thesis_effort == "high"
        assert cfg.portfolio_committee_model == "gpt-6.2-sol"
        assert cfg.portfolio_committee_effort == "high"
        assert cfg.bounded_model == "gpt-6-luna"
        assert cfg.bounded_effort == "high"
        assert cfg.chatgpt_plan_auth_path == "/opt/data/private/eventedge/credentials/plan-auth.json"
        assert cfg.blocked_tickers == ("GOOGL", "GOOG")

        pl = cfg.paper_ledger
        assert pl.calendar == "XNYS"
        assert pl.pricing_version == "raw-alpaca-sip-v1"
        assert pl.cost_model_version == "equity-10bps-act365-v2"
        assert pl.execution_clock_version == "causal-xnys-open-stops-v2"
        assert pl.benchmark_symbols == ("SPY", "BIL", "VTI", "VT")
        assert pl.slippage_bps == "10"
        assert pl.commission_per_fill == "0"
        assert pl.other_fee_per_fill == "0"
        assert pl.margin_requirement == "1.50"
        assert pl.existing_short_missing_borrow_rate == "0.30"
        assert pl.idle_cash_yield_rate == "0"
        assert pl.margin_financing_rate == "0"
        assert pl.bar_max_age_hours == 24

        pp = cfg.portfolio_policy
        assert pp.version == "portfolio_policy_v2"
        assert pp.annualized_volatility_floor == 0.15
        assert pp.volatility_lookback_sessions == 60
        assert pp.options_overlays_enabled is False
        assert pp.max_open_chase_pct == 0.05

        rd = cfg.risk_discipline
        assert rd.reentry_cooldown_days == 7
        assert rd.regime_vix_stressed == 25.0
        assert rd.short_conviction_threshold == 0.60

        ss = cfg.short_selling
        assert ss.borrow_cost_reject_above == 0.05
        assert dict(ss.borrow_cost_tiers) == {"5": 0.005, "15": 0.02, "30": 0.05}
        assert ss.hard_to_borrow_si_pct == 30

        assert cfg == DEFAULT_GENERATION_CONFIG

    def test_roundtrip_canonical_json_and_sha256_pinning(self, tmp_path: Path) -> None:
        cfg = load_generation_config(GEN003_PROFILE)
        roundtripped = GenerationConfig.from_dict(cfg.to_dict())
        assert roundtripped == cfg
        assert roundtripped.canonical_json() == cfg.canonical_json()
        assert roundtripped.sha256() == cfg.sha256()
        assert len(cfg.sha256()) == 64

        # Verify expected_sha256 and expected_file_sha256 checks
        raw_sha = hashlib.sha256(GEN003_PROFILE.read_bytes()).hexdigest()
        loaded = load_generation_config(
            GEN003_PROFILE,
            expected_sha256=cfg.sha256(),
            expected_file_sha256=raw_sha,
        )
        assert loaded == cfg

        with pytest.raises(ConfigValidationError, match="canonical sha256 mismatch"):
            load_generation_config(GEN003_PROFILE, expected_sha256="0" * 64)
        with pytest.raises(ConfigValidationError, match="file sha256 mismatch"):
            load_generation_config(GEN003_PROFILE, expected_file_sha256="f" * 64)

        # Compact file produces same canonical sha256 despite different file bytes
        compact = tmp_path / "compact.json"
        compact.write_text(cfg.canonical_json(), encoding="utf-8")
        assert load_generation_config(compact, expected_sha256=cfg.sha256()) == cfg

    def test_rejects_unknown_keys_at_all_nesting_levels(self) -> None:
        base = GenerationConfig().to_dict()

        with pytest.raises(ConfigValidationError, match="unknown key"):
            GenerationConfig.from_dict({**base, "filing_research_policy": "disabled_v1"})

        for section in (
            "paper_ledger",
            "portfolio_policy",
            "risk_discipline",
            "short_selling",
            "risk_gate",
            "finnhub_reliability",
        ):
            mutated = {**base, section: {**base[section], "bogus_switch": True}}
            with pytest.raises(ConfigValidationError, match="unknown key"):
                GenerationConfig.from_dict(mutated)

    @pytest.mark.parametrize(
        "override",
        [
            {"enabled_strategies": ["litigation"]},
            {"enabled_strategies": ["earnings_call", "earnings_call"]},
            {"enabled_strategies": []},
            {"llm_billing_mode": "bedrock"},
            {"llm_billing_mode": "chatgpt_plan", "chatgpt_plan_auth_path": None},
            {"chatgpt_plan_auth_path": "relative/plan-auth.json"},
            {"thesis_effort": "ultra"},
            {"execution_mode": "live"},
            {"blocked_tickers": ["GOOG", "GOOG"]},
            {"paper_ledger": {"calendar": "XNAS"}},
            {"paper_ledger": {"slippage_bps": "-1"}},
            {"paper_ledger": {"margin_requirement": "0.50"}},
            {"paper_ledger": {"bar_max_age_hours": True}},
            {"portfolio_policy": {"version": "portfolio_policy_v1"}},
            {"portfolio_policy": {"max_open_chase_pct": 0.0}},
            {"portfolio_policy": {"annualized_volatility_floor": float("nan")}},
            {"risk_discipline": {"reentry_cooldown_days": True}},
            {"risk_discipline": {"short_conviction_threshold": 1.5}},
            {"short_selling": {"borrow_cost_tiers": {"5": 0.05, "15": 0.01}}},
            {"short_selling": {"borrow_cost_tiers": {"bad": 0.01}}},
            {"thesis_model": "sk-secret-key-1234"},
        ],
    )
    def test_rejects_invalid_values(self, override: dict) -> None:
        payload = GenerationConfig().to_dict()
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(payload.get(key), dict):
                payload[key] = {**payload[key], **value}
            else:
                payload[key] = value
        with pytest.raises(ConfigValidationError):
            GenerationConfig.from_dict(payload)

    def test_load_rejects_symlinks_and_duplicate_json_keys(self, tmp_path: Path) -> None:
        real = tmp_path / "real.json"
        real.write_text(GenerationConfig().canonical_json(), encoding="utf-8")
        link = tmp_path / "link.json"
        link.symlink_to(real)
        with pytest.raises(ConfigValidationError, match="symlink"):
            load_generation_config(link)

        dup = tmp_path / "dup.json"
        dup.write_text('{"thesis_model": "gpt-6.2-sol", "thesis_model": "gpt-6.2-sol"}', encoding="utf-8")
        with pytest.raises(ConfigValidationError, match="duplicate JSON key"):
            load_generation_config(dup)

    def test_default_config_is_clean_and_obsolete_profiles_removed(self) -> None:
        ar = DEFAULT_CONFIG["autoresearch"]
        assert DEFAULT_CONFIG["execution"]["mode"] == "paper"
        assert ar["enabled_strategies"] == list(ACTIVE_STRATEGIES)
        assert ar["portfolio_policy"]["max_open_chase_pct"] == 0.05
        assert ar["risk_discipline"]["reentry_cooldown_days"] == 7
        for dead_key in (
            "cache_model",
            "live_model",
            "strategist_model",
            "cro_model",
            "state_economics_policy",
            "edgar_user_agent",
            "regulations_api_key",
            "courtlistener_token",
            "fmp_api_key",
            "walk_forward_windows",
            "fast_backtest",
        ):
            assert dead_key not in ar
        assert "decision_shadow" not in DEFAULT_CONFIG
        assert not (REPO_ROOT / "configs").exists()
        for v in (1, 2, 3):
            assert not (REPO_ROOT / "config" / "profiles" / f"stabilization-v{v}.json").exists()


# ---------------------------------------------------------------------------
# 2. Shared orchestration _io.py
# ---------------------------------------------------------------------------


class TestOrchestrationIO:
    def test_atomic_write_text_and_json_permissions_and_replace(self, tmp_path: Path) -> None:
        txt_path = tmp_path / "nested" / "note.txt"
        atomic_write_text(txt_path, "first")
        assert txt_path.read_text(encoding="utf-8") == "first"
        assert stat.S_IMODE(txt_path.stat().st_mode) == 0o600

        atomic_write_text(txt_path, "second", mode=0o640)
        assert txt_path.read_text(encoding="utf-8") == "second"
        assert stat.S_IMODE(txt_path.stat().st_mode) == 0o640
        assert [p.name for p in txt_path.parent.iterdir()] == ["note.txt"]

        json_path = tmp_path / "data.json"
        atomic_write_json(json_path, {"b": 2, "a": 1})
        assert stat.S_IMODE(json_path.stat().st_mode) == 0o600
        assert load_json_strict(json_path) == {"a": 1, "b": 2}

        with pytest.raises(ValueError):
            atomic_write_json(json_path, {"bad": float("nan")})

        link = tmp_path / "sym.txt"
        link.symlink_to(txt_path)
        with pytest.raises(ValueError, match="symlink"):
            atomic_write_text(link, "blocked")

    def test_load_json_strict_rejects_duplicates_and_nonstandard_constants(
        self, tmp_path: Path
    ) -> None:
        assert load_json_strict('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}

        with pytest.raises(ValueError, match="duplicate JSON key"):
            load_json_strict('{"a": 1, "a": 2}')
        with pytest.raises(ValueError, match="duplicate JSON key"):
            load_json_strict('{"outer": {"k": 1, "k": 2}}')
        with pytest.raises(ValueError, match="invalid JSON constant"):
            load_json_strict('{"x": NaN}')
        with pytest.raises(ValueError, match="invalid JSON constant"):
            load_json_strict('{"x": Infinity}')

        p = tmp_path / "dup.json"
        p.write_text('{"x": 1, "x": 2}', encoding="utf-8")
        with pytest.raises(ValueError, match="duplicate JSON key"):
            load_json_strict(p)

        sym = tmp_path / "sym.json"
        sym.symlink_to(p)
        with pytest.raises(ValueError, match="symlink"):
            load_json_strict(sym)

    def test_open_sqlite_pragmas_closing_and_readonly(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.sqlite3"
        captured_conn: sqlite3.Connection | None = None
        with open_sqlite(db_path, readonly=False, busy_timeout_ms=10000) as conn:
            captured_conn = conn
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, val TEXT)")
            conn.execute("INSERT INTO items (id, val) VALUES (1, 'ok')")

        # closing() guarantees connection is closed upon context exit
        assert captured_conn is not None
        with pytest.raises(sqlite3.ProgrammingError):
            captured_conn.execute("SELECT 1")

        # Read-only enforces query_only=ON
        with open_sqlite(db_path, readonly=True) as ro:
            assert ro.execute("PRAGMA query_only").fetchone()[0] == 1
            assert ro.execute("SELECT val FROM items WHERE id = 1").fetchone()["val"] == "ok"
            with pytest.raises(sqlite3.OperationalError):
                ro.execute("INSERT INTO items (id, val) VALUES (2, 'nope')")

        # Exception rolls back uncommitted DML
        with pytest.raises(RuntimeError):
            with open_sqlite(db_path, readonly=False) as conn:
                conn.execute("INSERT INTO items (id, val) VALUES (2, 'rolled_back')")
                raise RuntimeError("boom")
        with open_sqlite(db_path, readonly=True) as ro:
            assert ro.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1

    def test_snapshot_sqlite_works_with_active_wal_writer(self, tmp_path: Path) -> None:
        src = tmp_path / "live.sqlite3"
        dst = tmp_path / "backups" / "snap.sqlite3"
        with open_sqlite(src, readonly=False) as writer:
            writer.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v INTEGER)")
            writer.execute("INSERT INTO t (k, v) VALUES ('a', 1), ('b', 2)")
            writer.commit()
            # WAL sidecar active while writer connection stays open
            writer.execute("INSERT INTO t (k, v) VALUES ('c', 3)")
            writer.commit()
            result = snapshot_sqlite(src, dst)
            assert result == dst

        assert stat.S_IMODE(dst.stat().st_mode) == 0o600
        with open_sqlite(dst, readonly=True) as snap_conn:
            rows = snap_conn.execute("SELECT k, v FROM t ORDER BY k").fetchall()
            assert [(r["k"], r["v"]) for r in rows] == [("a", 1), ("b", 2), ("c", 3)]

        with pytest.raises(ValueError, match="distinct"):
            snapshot_sqlite(src, src)

    def test_verify_git_worktree_host_and_container_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        head = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        info = verify_git_worktree(REPO_ROOT, expected_commit=head)
        assert info["verified"] is True
        assert info["mode"] == "git"
        assert info["commit"] == head

        # Short SHA prefix matches
        assert verify_git_worktree(REPO_ROOT, expected_commit=head[:8])["commit"] == head

        with pytest.raises(ValueError, match="commit mismatch"):
            verify_git_worktree(REPO_ROOT, expected_commit="0" * 40)

        # Container fallback in non-git directory
        non_git = tmp_path / "container_app"
        non_git.mkdir()
        monkeypatch.delenv("EVENTEDGE_GENERATION_COMMIT", raising=False)
        unverified = verify_git_worktree(non_git)
        assert unverified["verified"] is False
        assert unverified["mode"] == "unavailable"

        # Fail-closed when expected_commit is demanded without git or marker
        with pytest.raises(ValueError, match="unable to verify expected commit"):
            verify_git_worktree(non_git, expected_commit="a" * 40)

        # Container marker file works cleanly
        commit_hex = "a" * 40
        (non_git / ".generation-commit").write_text(commit_hex + "\n", encoding="utf-8")
        cinfo = verify_git_worktree(non_git, expected_commit=commit_hex)
        assert cinfo["verified"] is True
        assert cinfo["mode"] == "container"
        assert cinfo["commit"] == commit_hex

        with pytest.raises(ValueError, match="container commit mismatch"):
            verify_git_worktree(non_git, expected_commit="b" * 40)

    def test_canonical_cohorts_and_source_taxonomy(self) -> None:
        assert len(CANONICAL_COHORTS) == 16
        assert len(set(CANONICAL_COHORTS)) == 16
        assert COHORT_HORIZONS == ("30d", "3m", "6m", "1y")
        assert COHORT_SIZES == ("5k", "10k", "50k", "100k")
        assert CANONICAL_COHORTS[0] == "horizon_30d_size_5k"
        assert CANONICAL_COHORTS[-1] == "horizon_1y_size_100k"

        assert SOURCE_TAXONOMY.accounting_sources == frozenset(
            {"alpaca_sip", "yfinance_actions_benchmarks"}
        )
        assert SOURCE_TAXONOMY.strategy_sources == frozenset(
            {
                "finnhub",
                "yfinance_research",
                "usaspending",
                "noaa",
                "usda",
                "drought_monitor",
                "cftc",
                "fred",
            }
        )
        assert SOURCE_TAXONOMY.optional_enrichment == frozenset(
            {"yfinance_profile", "finra_short_interest", "ken_french"}
        )
        assert SOURCE_TAXONOMY.tier_for("alpaca_sip") == "tier1_accounting"
        assert SOURCE_TAXONOMY.tier_for("finnhub") == "tier2_strategy"
        assert SOURCE_TAXONOMY.tier_for("finra_short_interest") == "tier2_optional_enrichment"
        assert SOURCE_TAXONOMY.is_tier1("alpaca_sip") is True
        assert SOURCE_TAXONOMY.is_tier2("finnhub") is True

        for retired in ("openbb", "courtlistener", "congress", "regulations", "edgar"):
            assert retired not in SOURCE_TAXONOMY.all_sources
            with pytest.raises(KeyError):
                SOURCE_TAXONOMY.tier_for(retired)


# ---------------------------------------------------------------------------
# 3. Unified GenerationDB (generation.sqlite3)
# ---------------------------------------------------------------------------


class TestGenerationDB:
    def test_schema_tables_pragmas_and_cohort_indexing(self, tmp_path: Path) -> None:
        db = GenerationDB.for_state_dir(tmp_path)
        assert db.path == tmp_path / GENERATION_DB_FILENAME
        assert db.schema_version() == SCHEMA_VERSION
        assert db.check_integrity() is True

        tables = db.table_names()
        assert len(SHARED_TABLES) == 14
        assert len(COHORT_TABLES) == 17
        assert len(ALL_TABLES) == 31
        assert tables == ALL_TABLES

        with db.connect(readonly=False) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000

            # Every cohort-scoped table must carry cohort_id and have a PK or index on cohort_id
            for table in sorted(COHORT_TABLES):
                cols = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
                assert "cohort_id" in cols, f"missing cohort_id column on {table}"
                indexes = conn.execute(f"PRAGMA index_list({table})").fetchall()
                indexed_leading = set()
                for idx in indexes:
                    idx_cols = conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()
                    if idx_cols:
                        indexed_leading.add(idx_cols[0]["name"])
                pk_cols = [
                    row["name"]
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                    if row["pk"] == 1
                ]
                assert (
                    "cohort_id" in indexed_leading or pk_cols == ["cohort_id"]
                ), f"cohort_id not indexed on {table}"

            # Exact policy_audit_records columns
            audit_cols = [
                row["name"]
                for row in conn.execute("PRAGMA table_info(policy_audit_records)").fetchall()
            ]
            assert audit_cols == [
                "cohort_id",
                "record_type",
                "record_id",
                "session",
                "epoch_id",
                "payload_json",
                "payload_sha256",
                "created_at",
            ]

    def test_foreign_keys_and_transactions_enforced(self, tmp_path: Path) -> None:
        db = GenerationDB.for_state_dir(tmp_path)

        # Foreign key violation rejected on intent_signals -> signals
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                conn.execute(
                    """INSERT INTO order_intents (
                           intent_id, cohort_id, side, requested_qty, created_at,
                           eligible_session, price_rule, status
                       ) VALUES ('i1', 'horizon_30d_size_5k', 'buy', 10,
                                 '2026-11-10T21:00:00+00:00', '2026-11-11',
                                 'next_session_open', 'pending')"""
                )
                conn.execute(
                    """INSERT INTO intent_signals (
                           cohort_id, intent_id, signal_id, signal_order
                       ) VALUES ('horizon_30d_size_5k', 'i1', 'missing_signal', 0)"""
                )

        # Failed transaction rolled back order_intents insert too
        with db.connect(readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0] == 0

    def test_policy_audit_records_idempotency_conflict_and_tamper_detection(
        self, tmp_path: Path
    ) -> None:
        db = GenerationDB.for_state_dir(tmp_path)
        sha1 = db.write_policy_audit_record(
            cohort_id="horizon_30d_size_5k",
            record_type="staging_manifest",
            record_id="manifest_001",
            session="2026-11-10",
            epoch_id="epoch_001",
            payload={"approved_count": 2, "tickers": ["AAPL", "MSFT"]},
        )
        # Idempotent replay succeeds with identical digest
        sha2 = db.write_policy_audit_record(
            cohort_id="horizon_30d_size_5k",
            record_type="staging_manifest",
            record_id="manifest_001",
            session="2026-11-10",
            epoch_id="epoch_001",
            payload={"tickers": ["AAPL", "MSFT"], "approved_count": 2},
        )
        assert sha1 == sha2

        # Conflict on changed payload raises ValueError
        with pytest.raises(ValueError, match="conflict"):
            db.write_policy_audit_record(
                cohort_id="horizon_30d_size_5k",
                record_type="staging_manifest",
                record_id="manifest_001",
                session="2026-11-10",
                epoch_id="epoch_001",
                payload={"approved_count": 3, "tickers": ["AAPL", "MSFT"]},
            )

        rows = db.read_policy_audit_records(
            cohort_id="horizon_30d_size_5k",
            record_type="staging_manifest",
            session="2026-11-10",
        )
        assert len(rows) == 1
        assert rows[0]["payload"] == {"approved_count": 2, "tickers": ["AAPL", "MSFT"]}
        assert rows[0]["payload_sha256"] == sha1

        # Tamper detection on read
        with db.transaction() as conn:
            conn.execute(
                "UPDATE policy_audit_records SET payload_json = ? WHERE record_id = ?",
                ('{"approved_count":99}', "manifest_001"),
            )
        with pytest.raises(ValueError, match="digest mismatch"):
            db.read_policy_audit_records(cohort_id="horizon_30d_size_5k")

    def test_session_shared_seals_readonly_and_snapshot(self, tmp_path: Path) -> None:
        db = GenerationDB.for_state_dir(tmp_path)
        seal_sha = db.write_session_shared_seal(
            session="2026-11-10",
            epoch_id="epoch_001",
            generation_id="gen_003",
            config_sha256=GenerationConfig().sha256(),
            source_digest="1" * 64,
            regime_digest="2" * 64,
            signals_digest="3" * 64,
            marks_digest="4" * 64,
            payload={"signal_count": 5, "cohorts": list(CANONICAL_COHORTS)},
        )
        # Idempotent replay
        assert (
            db.write_session_shared_seal(
                session="2026-11-10",
                epoch_id="epoch_001",
                generation_id="gen_003",
                config_sha256=GenerationConfig().sha256(),
                source_digest="1" * 64,
                regime_digest="2" * 64,
                signals_digest="3" * 64,
                marks_digest="4" * 64,
                payload={"cohorts": list(CANONICAL_COHORTS), "signal_count": 5},
            )
            == seal_sha
        )
        # Conflicting seal raises ValueError
        with pytest.raises(ValueError, match="conflict"):
            db.write_session_shared_seal(
                session="2026-11-10",
                epoch_id="epoch_001",
                generation_id="gen_003",
                config_sha256=GenerationConfig().sha256(),
                source_digest="9" * 64,
                regime_digest="2" * 64,
                signals_digest="3" * 64,
                marks_digest="4" * 64,
                payload={"signal_count": 5, "cohorts": list(CANONICAL_COHORTS)},
            )

        ro = GenerationDB.open_readonly(db.path)
        seal = ro.read_session_shared_seal("2026-11-10")
        assert seal is not None
        assert seal["payload_sha256"] == seal_sha
        assert seal["generation_id"] == "gen_003"
        with pytest.raises(PermissionError):
            with ro.transaction():
                pass

        # Snapshot succeeds and preserves integrity
        snap_path = tmp_path / "snapshot.sqlite3"
        db.snapshot(snap_path)
        snap_db = GenerationDB.open_readonly(snap_path)
        assert snap_db.check_integrity() is True
        assert snap_db.read_session_shared_seal("2026-11-10") == seal
