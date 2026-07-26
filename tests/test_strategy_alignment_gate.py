import pytest

from core import feedback_batch_manager as manager


class Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"status": "matched", "candidates": [{"id": "k1"}]}


class Client:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, **kwargs):
        return Response()


@pytest.mark.asyncio
async def test_skill_target_gets_alignment_evidence(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    target = {"type": "SKILL", "artifact": "고객 만족 개선"}
    await manager._attach_alignment_evidence({"tenant_id": "t1"}, target)
    assert target["alignment_evidence"]["status"] == "matched"


@pytest.mark.asyncio
async def test_dmn_target_is_excluded(monkeypatch):
    target = {"type": "DMN_RULE", "artifact": {}}
    await manager._attach_alignment_evidence({"tenant_id": "t1"}, target)
    assert "alignment_evidence" not in target


@pytest.mark.asyncio
async def test_alignment_failure_does_not_block_proposal(monkeypatch):
    class FailingClient(Client):
        async def post(self, url, **kwargs):
            raise RuntimeError("offline")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FailingClient)
    target = {"type": "PROCESS_DEFINITION", "artifact": {"summary": "개선"}}
    await manager._attach_alignment_evidence(
        {"tenant_id": "t1", "proc_def_id": "p1"}, target
    )
    assert target["alignment_evidence"]["status"] == "unavailable"
