"""Independent sequential raw-original audit for supported source dispositions.

The caller must execute this inside its governed resource boundary and supply
one absolute-deadline callback returning finite positive remaining seconds.
The audit never establishes or resets deadlines, calls sources/models, or
certifies assessments/orders. Cooperative checks cannot preempt a blocked
library call; the caller's process boundary supplies that enforcement.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math

POLICY = 'verified_supported_documents_raw_audit_v1'


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),
        ensure_ascii=True,allow_nan=False).encode('ascii')).hexdigest()


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def validate_supported_raw_capture(data, config, raw_paths, *, check):
    """Audit every completed owner original, including rejected parse originals.

    `raw_paths` is a legacy complete private-path map or an exact governed
    original store bound to this inventory and inherited callback. Only one
    read-only original view is live at a time. Parser attestations remain
    provisional until this independently regenerates them from exact bytes.
    A successful audit can still contain failed/deferred source dispositions.
    """
    try:
        _require(callable(check), 'missing_raw_audit_deadline')
        def bounded_check():
            remaining=check()
            _require(type(remaining) in (int,float) and math.isfinite(remaining)
                and remaining>0, 'raw_audit_deadline_exhausted')
            return remaining
        bounded_check()
        from tradingagents.strategies.data_sources.filing_supported_policy import (
            POLICY as SOURCE_POLICY, build_local_source_proof,
        )
        _require(config.get('filing_material_policy')==SOURCE_POLICY, 'unsupported_raw_audit_policy')
        from .filing_acquisition_validation import validate_filing_acquisition_policy
        from .filing_material_validation import validate_filing_material_policy, COLLECTIONS
        acquisition=validate_filing_acquisition_policy(data,config)
        bounded_check()
        material=validate_filing_material_policy(data,config)
        bounded_check()
        graph=data['edgar']['filing_evidence']
        _require(acquisition is not None and material is not None, 'missing_raw_audit_scope')
        originals={entry['identity']['accession']:entry for entry in graph['acquisition_scope']['originals']}
        from tradingagents.strategies.data_sources.filing_original_store import RawOriginalStore, GzipOriginalStore
        if isinstance(raw_paths,dict):
            store=RawOriginalStore(originals,raw_paths,check=check)
        else:
            _require(type(raw_paths) in (RawOriginalStore,GzipOriginalStore), 'untrusted_original_store')
            store=raw_paths
            _require(store.expected_originals==originals, 'original_store_inventory_mismatch')
            _require(store.check_callback is check, 'original_store_deadline_mismatch')
        store.assert_closed()
        expected_facts={acc:e['ownership_facts'] for acc,e in graph['corpus'].items() if 'ownership_facts' in e}
        _require(graph.get('ownership_facts',{})==expected_facts, 'ownership_facts_inventory_mismatch')
        verified={}
        quarantine_count=0
        for accession,original in sorted(originals.items()):
            bounded_check()
            with store.open_original(accession) as raw:
                evidence=graph['corpus'].get(accession)
                if evidence is not None:
                    from tradingagents.strategies.data_sources.filing_evidence import frame_submission
                    identity=original['identity']
                    if 'material_quarantine' in evidence:
                        from tradingagents.strategies.data_sources.filing_material_policy import quarantine_evidence
                        framed=frame_submission(raw,expected_accession=accession,expected_form=identity['form'],
                            expected_date=identity['filing_date'],observed_at=original['observed_at'],
                            max_submission_bytes=512*1024**2,check=bounded_check,
                            inventory_reconciliation=evidence.get('inventory_reconciliation'))
                        expected=quarantine_evidence(framed,submission_size=len(raw),policy=SOURCE_POLICY,
                            required_exhibits=evidence['material_quarantine']['required_exhibits'])
                        _require(expected==evidence, 'raw_quarantine_evidence_mismatch')
                        quarantine_count+=1
                    else:
                        supplied=graph['supported_source_proofs'][accession]
                        proof=build_local_source_proof(evidence,original=original,raw_submission=raw,
                            required_exhibits=supplied['required_exhibits'],check=bounded_check)
                        _require(proof==supplied, 'raw_local_source_proof_mismatch')
                        verified[accession]=proof
                        encoded=evidence.get('ownership_primary_body')
                        if encoded is not None:
                            primaries=[u for u in evidence['units'] if u['selection']=='primary']
                            _require(len(primaries)==1, 'raw_ownership_primary_missing')
                            primary=primaries[0]
                            body=raw[primary['body_start']:primary['body_end']]
                            bounded_check()
                            _require(base64.b64decode(encoded,validate=True)==body, 'raw_ownership_primary_mismatch')
                            if 'ownership_facts' in evidence:
                                from tradingagents.strategies.data_sources.filing_ownership_lineage import validate_ownership_facts
                                validate_ownership_facts(evidence['ownership_facts'],evidence,body,check=bounded_check)
                bounded_check()
        bounded_check()
        from .filing_supported_scope import ownership_bundle,build_supported_dispositions
        from tradingagents.strategies.data_sources.filing_ownership_lineage import validate_ownership_lineage
        transient={**graph,'supported_source_proofs':verified}
        for accession,receipt in sorted(graph.get('ownership_lineages',{}).items()):
            bounded_check()
            bundle=ownership_bundle(transient,accession,check=bounded_check)
            validate_ownership_lineage(receipt,graph['corpus'][accession],
                **{key:value for key,value in bundle.items() if key!='receipt'},check=bounded_check)
        collections=graph.get('collections',{key:data['edgar'].get(key,[]) for key in COLLECTIONS})
        dispositions=build_supported_dispositions(transient,collections,check=bounded_check)
        _require(dispositions==graph['supported_dispositions'], 'raw_supported_disposition_mismatch')
        store.assert_closed()
        store.recheck_persistent_identities()
        bounded_check()
        original_store_stats=store.sealed_stats()
        return {'policy':POLICY,'raw_audit_complete':True,'completed_originals':len(originals),
            'completed_original_bytes':sum(entry['size'] for entry in originals.values()),
            'verified_source_proofs':len(verified),'quarantined_originals':quarantine_count,
            'failed_evidence_originals':len(set(originals)-set(graph['corpus'])),
            'original_manifest_sha256':_digest([originals[key] for key in sorted(originals)]),
            'dispositions_sha256':_digest(dispositions),'source_proofs_sha256':_digest(verified),
            'assessment_status':'not_certified','order_authority':False,
            'original_full_analysis_coverage':False,'original_store':original_store_stats}
    except Exception as error:
        raise ValueError('invalid_supported_raw_capture') from error
