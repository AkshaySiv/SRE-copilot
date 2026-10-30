import pytest

from app.services.intent import classify


@pytest.mark.parametrize("prompt", [
    "what is causing application slowness", "Why is the payments service slow today?", "checkout is failing",
    "sre-demo app is down", "Customers can't pay", "frontend returns 503", "backend pod keeps crashing",
    "root cause of the outage last night", "orders api not responding",
])
def test_incidents(prompt):
    assert classify(prompt) == "incident"


@pytest.mark.parametrize("prompt", [
    "Is anything broken in the cluster right now?", "Which pods restarted the most today, and why?",
    "How many nodes do I have?", "What was deployed in the last few hours?", "list all namespaces",
    "Are any persistent volumes almost full?", "Is disk space OK on all my VMs?",
])
def test_questions(prompt):
    assert classify(prompt) == "question"
