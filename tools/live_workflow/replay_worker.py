"""Separate offline accepted-state processor; never execute recording .pth hooks.

The supervisor mounts a coherent private campaign copy at its original absolute
path and provides a new empty output directory, kernel network denial, read-only
dependencies/tooling and no auth mount. Its parent verifies the original baseline
and copied contract again after this process exits. The lease below is solely the
native canonical lease in that private copy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sys


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('root','output-dir','session','contract-sha256','worker-sha256','uname-sha256'):
        parser.add_argument('--'+key,required=True)
    parser.add_argument('--credential-env',action='append',default=[],metavar='SOURCE_NAME',
        help='Explicit allowlisted source credential name; its existing value stays in memory only.')
    args=parser.parse_args(argv)
    # A base interpreter with -I -S is essential: the used campaign's immutable
    # venv .pth still belongs to the original recording and failed first replay.
    if not sys.flags.isolated or not sys.flags.no_site:
        raise RuntimeError('external_replay_requires_isolated_no_site_python')
    tool=Path(__file__).absolute();tool_root=tool.parents[2]
    if any(path.is_symlink() for path in (tool,*tool.parents)):
        raise RuntimeError('external_replay_worker_symlink')
    if hashlib.sha256(tool.read_bytes()).hexdigest()!=args.worker_sha256:
        raise RuntimeError('external_replay_worker_hash_mismatch')
    sys.path.insert(0,str(tool_root))
    from tools.live_workflow import runner
    root=runner.no_symlinks(args.root);output=runner.no_symlinks(args.output_dir)
    runner.require(output.is_dir() and not any(output.iterdir()),'external_replay_empty_output_required')
    runner.require(output.stat().st_uid==os.getuid() and output.stat().st_mode & 0o077==0,
        'external_replay_private_output_required')
    runner.require(not output.is_relative_to(root) and not root.is_relative_to(output),
        'external_replay_output_must_be_separate')
    attempts=None
    def deadline(*unused):
        raise RuntimeError('external_replay_deadline_exhausted')
    signal.signal(signal.SIGALRM,deadline);signal.setitimer(signal.ITIMER_REAL,115)
    try:
        credentials=runner.selected_credential_environment(args.credential_env)
        # Verification is stdlib/tool-only. It intentionally precedes the strict
        # process gate because native checkout validation uses other local Git
        # identity reads. No application or dependency SDK has been imported.
        runner.require(not any(name=='tradingagents' or name.startswith('tradingagents.')
            for name in sys.modules),'external_replay_application_preimported')
        runner.require(runner.sha(root/'contract.json')==args.contract_sha256,
            'external_replay_contract_hash_mismatch')
        contract,row=runner.verify_contract(root)
        uname_identity=runner.readonly_uname_identity(args.uname_sha256)
        runner.require(args.session==contract['reference_session'],'reference_session_changed')
        hook_config_sha256=runner.sha(root/'hook-config.json')
        identity={'schema_version':1,'kind':'external_accepted_state_replay_worker',
            'contract_sha256':args.contract_sha256,'native_recording_hooks':contract['hooks'],
            'hook_config_sha256':hook_config_sha256,'dependency_sha256':contract['dependency_sha256'],
            'local_uname':uname_identity,
            'candidate_commit':contract['candidate_commit'],'session':args.session,
            'credential_env_names':sorted(credentials),
            'native_lease_scope':'private_campaign_copy_only',
            'original_baseline_preservation':'parent_shared_lease_snapshot_and_postverification_required',
            'tool_sha256':{str(path.relative_to(tool_root)):runner.sha(path) for path in
                (tool,tool.with_name('runner.py'),tool.with_name('bootstrap.py'))}}
        runner.private_json(output/'replay-attempt.json',identity)
        # Only selected source credentials survive native identity checks; paid
        # routes, model/notification tokens and proxies are dropped before imports.
        env=runner.sanitized_environment(root,{},credentials)
        for name in ('home','cache','tmp','logs'):
            (output/name).mkdir(mode=0o700)
        env.update({'PATH':'/usr/bin:/bin','HOME':str(output/'home'),
            'XDG_CONFIG_HOME':str(output/'home/.config'),'XDG_CACHE_HOME':str(output/'cache'),
            'XDG_DATA_HOME':str(output/'home/.local/share'),'TMPDIR':str(output/'tmp'),
            'EVENTEDGE_LOG_DIR':str(output/'logs')})
        os.environ.clear();os.environ.update(env)
        sys.path[:0]=[str(root/'repo'),contract['dependency_site']]
        from tools.live_workflow import bootstrap
        bootstrap._install_log_redaction()
        attempts=bootstrap._replay_attempts()
        bootstrap._forbid_io(attempts,repo=root/'repo',git_identity=contract['local_git'],
            dependency_site=contract['dependency_site'],uname_identity=uname_identity)
        from tradingagents.strategies.data_sources.openbb_source import OpenBBSource
        runner.require(OpenBBSource().is_available(),'external_replay_openbb_unavailable')
        bootstrap._accepted_replay(root,args.session,output_dir=output,dependency_site=contract['dependency_site'],
            uname_identity=uname_identity)
        # Never disable the guard for post-verification. The trusted parent does
        # full checkout verification after exit; these direct reads keep the
        # original recording contract/hook bytes bound to this receipt.
        runner.require(runner.sha(root/'contract.json')==args.contract_sha256
            and runner.sha(root/'hook-config.json')==hook_config_sha256,'external_replay_contract_changed')
        hooks={str(path.relative_to(root/'hooks')):runner.sha(path)
            for path in sorted((root/'hooks').rglob('*.py'))}
        runner.require(hooks==contract['hooks'],'external_replay_recording_hooks_changed')
        runner.private_json(output/'replay-worker.json',{**identity,'success':True,
            'openbb_dependency_available':True,'accepted_replay_sha256':runner.sha(output/'accepted-replay.json')})
        return 0
    except Exception as error:
        import traceback
        runner.private_json(output/'replay-worker-failure.json',{'schema_version':1,'success':False,
            'failure_type':type(error).__name__,'attempted_calls':dict(attempts or {}),
            'failure_reason':str(error) if isinstance(error,runner.AcceptanceError) else 'external_replay_failed',
            'frames':[{'file':frame.filename,'function':frame.name,'line':frame.lineno}
                for frame in traceback.extract_tb(error.__traceback__)[-10:]],
            'worker_sha256':args.worker_sha256,'contract_sha256':args.contract_sha256})
        return 2
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)


if __name__=='__main__':
    raise SystemExit(main())
