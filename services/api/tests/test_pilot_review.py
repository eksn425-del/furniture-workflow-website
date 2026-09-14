import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routes.control_plane import act_on_review
from app.database import Database
from app.models import ProductionJob, ReviewQueueItem
from app.schemas import HumanReviewAction, ControlJobEdit
from packages.workflow_core.candidate_pool import CandidatePoolStore, CandidateRecord


@pytest.mark.parametrize('action', ['ACCEPT', 'CONFIRM', 'EDIT', 'REQUEST_RESCAN'])
@pytest.mark.parametrize('case', ['missing', 'stale', 'swapped_bytes', 'valid'])
def test_generic_review_binds_snapshot_current_and_seen_media(tmp_path, action, case):
    database = Database(tmp_path / 'control.sqlite3')
    database.create_schema()
    media = tmp_path / 'image.png'
    media.write_bytes(b'captured-image')
    digest = hashlib.sha256(media.read_bytes()).hexdigest()
    pool_path = tmp_path / 'pool.json'
    pool = CandidatePoolStore(pool_path, order_id='job', job_id='job')
    pool.add_candidates([CandidateRecord(candidate_id='candidate', order_id='job', job_id='job', record_id='record', source='example.test', source_product_id='sku', canonical_url='https://example.test/product', preview_id='preview', preview_url='https://example.test/image.png', capture_sha256=digest, image_sha256=digest, lineage={'media_sha256': digest, 'media_path': str(media)})])
    with database.session_factory() as session:
        session.add(ProductionJob(job_id='job', source_url='https://example.test/', site_key='example.test', title='Review test', goal='Test', target_mode='EXACT_N', target_value=1, requested_count=1, provider='OFF', candidate_pool_path=str(pool_path)))
        session.commit()
        session.add(ReviewQueueItem(review_id='review', job_id='job', record_id='record', reason_code='VISUAL_REVIEW', title='Review', detail='Test', evidence_json=json.dumps({'media_sha256': 'b' * 64 if case == 'stale' else digest})))
        session.commit()
    if case == 'swapped_bytes':
        media.write_bytes(b'different-image')
    payload = HumanReviewAction(action=action, reviewed_media_sha256=None if case == 'missing' else digest)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=database)))
    try:
        if case == 'valid':
            assert act_on_review('review', payload, request)['status'] == 'RESOLVED'
            with pytest.raises(HTTPException) as error:
                act_on_review('review', payload, request)
            assert error.value.status_code == 409
        else:
            with pytest.raises(HTTPException) as error:
                act_on_review('review', payload, request)
            assert error.value.status_code == 409
            with database.session_factory() as session:
                assert session.get(ReviewQueueItem, 'review').status == 'OPEN'
    finally:
        database.dispose()


def test_dimension_ui_options_are_real_backend_policy_values():
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    ui = (root / 'apps/web/components/production-console.tsx').read_text(encoding='utf-8')
    for value in ('FULL_ONLY', 'ALLOW_PARTIAL_ANCHOR'):
        assert ControlJobEdit(dimension_anchor_policy=value).dimension_anchor_policy == value
        assert f'<option value="{value}">' in ui
    assert 'dimension_anchor_policy: dimensionPolicy' in ui
