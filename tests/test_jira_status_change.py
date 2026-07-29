# Tests Jira ticket transitions

import os

import pytest

from app.services.jira import JiraClient


def test_manual_ticket_transition():
    if os.getenv("RUN_JIRA_INTEGRATION_TESTS") != "1":
        pytest.skip("Set RUN_JIRA_INTEGRATION_TESTS=1 to run live Jira transition test")

    # Initialize client 
    client = JiraClient()
    
    target_ticket = os.getenv("JIRA_TEST_TICKET", "").strip()
    target_status = os.getenv("JIRA_TEST_TARGET_STATUS", "").strip()
    if not target_ticket or not target_status:
        pytest.skip("Set JIRA_TEST_TICKET and JIRA_TEST_TARGET_STATUS for integration transition test")
    
    print(f"Initializing status transition test for ticket: {target_ticket}")
    
    # Invoke the function by passing the key and target status
    success = client.transition_issue(issue_key=target_ticket, target_status_name=target_status)
    assert success is True

if __name__ == "__main__":
    test_manual_ticket_transition()