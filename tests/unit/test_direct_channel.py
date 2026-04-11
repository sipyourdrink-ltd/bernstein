import time
from pathlib import Path

from bernstein.core.bulletin import ChannelQuery, ChannelResponse, DirectChannel


def test_post_query_and_respond() -> None:
    """Test posting a query and receiving a response."""
    ch = DirectChannel()

    q = ch.post_query(
        sender_agent="agent-fe",
        topic="api-schema",
        content="What response schema did you use for /api/users?",
        target_agent="agent-be",
    )
    assert q.id
    assert q.sender_agent == "agent-fe"
    assert q.topic == "api-schema"
    assert q.target_agent == "agent-be"
    assert not q.resolved
    assert q.expires_at > q.timestamp

    r = ch.post_response(q.id, "agent-be", '{"users": [{"id": "str", "name": "str"}]}')
    assert r is not None
    assert r.query_id == q.id
    assert r.responder_agent == "agent-be"

    responses = ch.get_responses(q.id)
    assert len(responses) == 1
    assert responses[0].id == r.id


def test_query_marked_resolved_on_response() -> None:
    """Test that posting a response marks the query as resolved."""
    ch = DirectChannel()
    q = ch.post_query("a1", "topic", "question?")
    ch.post_response(q.id, "a2", "answer")

    pending = ch.get_pending_queries()
    assert all(p.id != q.id for p in pending)


def test_response_to_unknown_query_returns_none() -> None:
    """Test that responding to a non-existent query returns None."""
    ch = DirectChannel()
    r = ch.post_response("nonexistent", "a1", "answer")
    assert r is None


def test_get_pending_queries_by_agent_id() -> None:
    """Test filtering pending queries by target agent."""
    ch = DirectChannel()
    q1 = ch.post_query("a1", "t1", "for agent-be", target_agent="agent-be")
    q2 = ch.post_query("a1", "t2", "for agent-fe", target_agent="agent-fe")
    ch.post_query("a1", "t3", "for agent-qa", target_agent="agent-qa")

    pending = ch.get_pending_queries(agent_id="agent-be")
    assert len(pending) == 1
    assert pending[0].id == q1.id

    pending_fe = ch.get_pending_queries(agent_id="agent-fe")
    assert len(pending_fe) == 1
    assert pending_fe[0].id == q2.id


def test_get_pending_queries_by_role() -> None:
    """Test filtering pending queries by target role."""
    ch = DirectChannel()
    q1 = ch.post_query("a1", "t1", "for backend role", target_role="backend")
    ch.post_query("a1", "t2", "for frontend role", target_role="frontend")

    pending = ch.get_pending_queries(role="backend")
    assert len(pending) == 1
    assert pending[0].id == q1.id


def test_get_pending_queries_broadcast() -> None:
    """Test that untargeted queries appear for any agent/role filter."""
    ch = DirectChannel()
    q = ch.post_query("a1", "general", "anyone know the deploy status?")

    pending_no_filter = ch.get_pending_queries()
    assert len(pending_no_filter) == 1
    assert pending_no_filter[0].id == q.id


def test_targeted_queries_excluded_from_broadcast() -> None:
    """Test that targeted queries don't appear in unrelated queries."""
    ch = DirectChannel()
    ch.post_query("a1", "t1", "for agent-be only", target_agent="agent-be")

    pending = ch.get_pending_queries(agent_id="agent-fe")
    assert len(pending) == 0


def test_expiry_and_cleanup() -> None:
    """Test that expired queries are cleaned up."""
    ch = DirectChannel()
    ch.post_query("a1", "t1", "will expire", ttl_seconds=0.01)
    time.sleep(0.05)

    removed = ch.cleanup_expired()
    assert removed == 1
    assert ch.count == 0


def test_resolved_queries_not_cleaned_up() -> None:
    """Test that expired-but-resolved queries survive cleanup."""
    ch = DirectChannel()
    q = ch.post_query("a1", "t1", "will expire but resolved", ttl_seconds=0.01)
    ch.post_response(q.id, "a2", "answer")
    time.sleep(0.05)

    removed = ch.cleanup_expired()
    assert removed == 0
    assert ch.count == 1


def test_get_conversation_by_topic() -> None:
    """Test finding all queries on a given topic."""
    ch = DirectChannel()
    ch.post_query("a1", "api-schema", "what schema for /users?")
    ch.post_query("a2", "api-schema", "what schema for /orders?")
    ch.post_query("a3", "deploy", "when is next deploy?")

    conv = ch.get_conversation("api-schema")
    assert len(conv) == 2
    assert all(q.topic == "api-schema" for q in conv)

    deploy = ch.get_conversation("deploy")
    assert len(deploy) == 1


def test_multiple_responses_to_same_query() -> None:
    """Test that multiple agents can respond to the same query."""
    ch = DirectChannel()
    q = ch.post_query("a1", "t1", "what port are you using?")

    ch.post_response(q.id, "a2", "8080")
    ch.post_response(q.id, "a3", "8081")

    responses = ch.get_responses(q.id)
    assert len(responses) == 2
    assert responses[0].content == "8080"
    assert responses[1].content == "8081"


def test_persistence_round_trip(tmp_path: Path) -> None:
    """Test flushing to and loading from JSONL."""
    ch1 = DirectChannel()
    q = ch1.post_query("a1", "schema", "what schema?", target_role="backend")
    ch1.post_response(q.id, "a2", "the answer")

    path = tmp_path / "channel.jsonl"
    written = ch1.flush_to_disk(path)
    assert written == 2  # 1 query + 1 response
    assert path.exists()

    ch2 = DirectChannel()
    loaded = ch2.load_from_disk(path)
    assert loaded == 2

    assert ch2.count == 1
    queries = ch2.get_conversation("schema")
    assert len(queries) == 1
    assert queries[0].sender_agent == "a1"

    responses = ch2.get_responses(q.id)
    assert len(responses) == 1
    assert responses[0].content == "the answer"


def test_persistence_skips_duplicates(tmp_path: Path) -> None:
    """Test that loading the same file twice doesn't create duplicates."""
    ch = DirectChannel()
    ch.post_query("a1", "t1", "question")

    path = tmp_path / "channel.jsonl"
    ch.flush_to_disk(path)

    loaded_first = ch.load_from_disk(path)
    assert loaded_first == 0  # already in memory

    ch2 = DirectChannel()
    ch2.load_from_disk(path)
    ch2.load_from_disk(path)
    assert ch2.count == 1


def test_get_responses_empty() -> None:
    """Test getting responses for a query with no responses."""
    ch = DirectChannel()
    q = ch.post_query("a1", "t1", "hello?")
    responses = ch.get_responses(q.id)
    assert responses == []


def test_get_responses_unknown_query() -> None:
    """Test getting responses for a non-existent query."""
    ch = DirectChannel()
    responses = ch.get_responses("nonexistent")
    assert responses == []


def test_channel_query_dataclass_round_trip() -> None:
    """Test ChannelQuery to_dict/from_dict round-trip."""
    q = ChannelQuery(
        sender_agent="a1",
        topic="test",
        content="question",
        target_agent="a2",
        target_role=None,
        expires_at=99999.0,
        resolved=True,
    )
    d = q.to_dict()
    q2 = ChannelQuery.from_dict(d)
    assert q2.id == q.id
    assert q2.sender_agent == q.sender_agent
    assert q2.topic == q.topic
    assert q2.content == q.content
    assert q2.target_agent == q.target_agent
    assert q2.target_role == q.target_role
    assert q2.expires_at == q.expires_at
    assert q2.resolved == q.resolved


def test_channel_response_dataclass_round_trip() -> None:
    """Test ChannelResponse to_dict/from_dict round-trip."""
    r = ChannelResponse(
        query_id="q1",
        responder_agent="a2",
        content="answer",
    )
    d = r.to_dict()
    r2 = ChannelResponse.from_dict(d)
    assert r2.id == r.id
    assert r2.query_id == r.query_id
    assert r2.responder_agent == r.responder_agent
    assert r2.content == r.content
    assert r2.timestamp == r.timestamp
