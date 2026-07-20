#!/usr/bin/env python3
"""
Test script for queue, heartbeat, and lease recovery functionality.

Run from your Django project directory (e.g., agent_studio):
    source ~/.virtualenvs/agent_studio/bin/activate
    python ../test_queue_heartbeat.py

Or from agent_libraries root:
    source ~/.virtualenvs/agent_studio/bin/activate
    python test_queue_heartbeat.py
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from pathlib import Path

# Add agent_studio to path so Django can find settings
script_dir = Path(__file__).parent
agent_studio_dir = script_dir / "agent_studio"
if agent_studio_dir.exists():
    sys.path.insert(0, str(agent_studio_dir))

# Setup Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agent_studio.settings.dev")

import django
django.setup()

from django_agent_runtime.models import AgentRun, AgentConversation
from django_agent_runtime.models.base import RunStatus
from django_agent_runtime.runtime.queue.postgres import PostgresQueue


async def test_queue_claim():
    """Test basic queue claim functionality."""
    print("\n" + "="*60)
    print("TEST 1: Queue Claim")
    print("="*60)
    
    queue = PostgresQueue(lease_ttl_seconds=30)
    worker_id = f"test-worker-{uuid4().hex[:8]}"
    
    # Create a test conversation and run
    conv = await asyncio.to_thread(
        AgentConversation.objects.create,
        agent_key="test-agent",
    )
    
    run = await asyncio.to_thread(
        AgentRun.objects.create,
        conversation=conv,
        agent_key="test-agent",
        status=RunStatus.QUEUED,
        input={"messages": [{"role": "user", "content": "test"}]},
    )
    
    print(f"✅ Created test run: {run.id}")
    
    # Claim the run
    claimed = await queue.claim(worker_id=worker_id, batch_size=1)
    
    if claimed:
        print(f"✅ Claimed run: {claimed[0].run_id}")
        print(f"   Agent key: {claimed[0].agent_key}")
        print(f"   Lease expires: {claimed[0].lease_expires_at}")
        
        # Release it
        await queue.release(claimed[0].run_id, worker_id, success=True, output={"test": "passed"})
        print(f"✅ Released run successfully")
    else:
        print("❌ Failed to claim run")
    
    # Cleanup
    await asyncio.to_thread(run.delete)
    await asyncio.to_thread(conv.delete)
    
    return bool(claimed)


async def test_lease_extension():
    """Test heartbeat/lease extension."""
    print("\n" + "="*60)
    print("TEST 2: Lease Extension (Heartbeat)")
    print("="*60)
    
    queue = PostgresQueue(lease_ttl_seconds=10)
    worker_id = f"test-worker-{uuid4().hex[:8]}"
    
    # Create test run
    conv = await asyncio.to_thread(
        AgentConversation.objects.create,
        agent_key="test-agent",
    )
    
    run = await asyncio.to_thread(
        AgentRun.objects.create,
        conversation=conv,
        agent_key="test-agent",
        status=RunStatus.QUEUED,
        input={"messages": []},
    )
    
    # Claim
    claimed = await queue.claim(worker_id=worker_id, batch_size=1)
    if not claimed:
        print("❌ Failed to claim run")
        return False
    
    original_expires = claimed[0].lease_expires_at
    print(f"✅ Claimed run, lease expires: {original_expires}")
    
    # Wait a bit then extend
    await asyncio.sleep(2)
    
    extended = await queue.extend_lease(run.id, worker_id, seconds=30)
    
    if extended:
        # Check new expiry
        updated_run = await asyncio.to_thread(AgentRun.objects.get, id=run.id)
        print(f"✅ Lease extended!")
        print(f"   Original expiry: {original_expires}")
        print(f"   New expiry: {updated_run.lease_expires_at}")
        print(f"   Extended by: {(updated_run.lease_expires_at - original_expires).seconds}s")
    else:
        print("❌ Failed to extend lease")
    
    # Cleanup
    await queue.release(run.id, worker_id, success=True)
    await asyncio.to_thread(run.delete)
    await asyncio.to_thread(conv.delete)
    
    return extended


async def test_expired_lease_recovery():
    """Test recovery of expired leases."""
    print("\n" + "="*60)
    print("TEST 3: Expired Lease Recovery")
    print("="*60)
    
    queue = PostgresQueue(lease_ttl_seconds=30)
    
    # Create a run with an already-expired lease (simulating worker crash)
    conv = await asyncio.to_thread(
        AgentConversation.objects.create,
        agent_key="test-agent",
    )
    
    expired_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    
    run = await asyncio.to_thread(
        AgentRun.objects.create,
        conversation=conv,
        agent_key="test-agent",
        status=RunStatus.RUNNING,  # Stuck in running
        lease_owner="dead-worker",
        lease_expires_at=expired_time,  # Expired 5 mins ago
        input={"messages": []},
        attempt=1,
        max_attempts=3,
    )
    
    print(f"✅ Created run with expired lease (expired at {expired_time})")
    
    # Run recovery
    recovered = await queue.recover_expired_leases()
    
    print(f"✅ Recovery found {recovered} expired lease(s)")
    
    # Check the run status
    updated_run = await asyncio.to_thread(AgentRun.objects.get, id=run.id)
    print(f"   Run status after recovery: {updated_run.status}")
    print(f"   Run attempt: {updated_run.attempt}")
    
    # Cleanup
    await asyncio.to_thread(run.delete)
    await asyncio.to_thread(conv.delete)

    return recovered > 0


async def test_cancellation():
    """Test run cancellation."""
    print("\n" + "="*60)
    print("TEST 4: Run Cancellation")
    print("="*60)

    queue = PostgresQueue(lease_ttl_seconds=30)
    worker_id = f"test-worker-{uuid4().hex[:8]}"

    # Create test run
    conv = await asyncio.to_thread(
        AgentConversation.objects.create,
        agent_key="test-agent",
    )

    run = await asyncio.to_thread(
        AgentRun.objects.create,
        conversation=conv,
        agent_key="test-agent",
        status=RunStatus.QUEUED,
        input={"messages": []},
    )

    print(f"✅ Created test run: {run.id}")

    # Claim it
    claimed = await queue.claim(worker_id=worker_id, batch_size=1)
    if not claimed:
        print("❌ Failed to claim run")
        return False

    print(f"✅ Claimed run")

    # Request cancellation
    cancelled = await queue.cancel(run.id)
    print(f"✅ Cancellation requested: {cancelled}")

    # Check if cancelled
    is_cancelled = await queue.is_cancelled(run.id)
    print(f"✅ is_cancelled check: {is_cancelled}")

    # Cleanup
    await queue.release(run.id, worker_id, success=False, error={"type": "Cancelled"})
    await asyncio.to_thread(run.delete)
    await asyncio.to_thread(conv.delete)

    return cancelled and is_cancelled


async def test_queue_stats():
    """Test queue stats by counting runs in different states."""
    print("\n" + "="*60)
    print("TEST 5: Queue Stats")
    print("="*60)

    # Get current counts
    def get_counts():
        return {
            "queued": AgentRun.objects.filter(status=RunStatus.QUEUED).count(),
            "running": AgentRun.objects.filter(status=RunStatus.RUNNING).count(),
            "succeeded": AgentRun.objects.filter(status=RunStatus.SUCCEEDED).count(),
            "failed": AgentRun.objects.filter(status=RunStatus.FAILED).count(),
            "timed_out": AgentRun.objects.filter(status=RunStatus.TIMED_OUT).count(),
            "cancelled": AgentRun.objects.filter(status=RunStatus.CANCELLED).count(),
        }

    counts = await asyncio.to_thread(get_counts)

    print("📊 Current queue stats:")
    for status, count in counts.items():
        print(f"   {status}: {count}")

    total = sum(counts.values())
    print(f"   TOTAL: {total}")

    return True


async def main():
    """Run all tests."""
    print("\n" + "🧪"*30)
    print("  QUEUE & HEARTBEAT TEST SUITE")
    print("🧪"*30)

    results = {}

    try:
        results["claim"] = await test_queue_claim()
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        results["claim"] = False

    try:
        results["lease_extension"] = await test_lease_extension()
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        results["lease_extension"] = False

    try:
        results["expired_recovery"] = await test_expired_lease_recovery()
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        results["expired_recovery"] = False

    try:
        results["cancellation"] = await test_cancellation()
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        results["cancellation"] = False

    try:
        results["stats"] = await test_queue_stats()
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        results["stats"] = False

    # Summary
    print("\n" + "="*60)
    print("📋 TEST SUMMARY")
    print("="*60)

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for test, result in results.items():
        icon = "✅" if result else "❌"
        print(f"  {icon} {test}")

    print(f"\n  {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 All tests passed!")
    else:
        print("\n⚠️  Some tests failed")

    return passed == total


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)

