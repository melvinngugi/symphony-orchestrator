# Tests Jira ticket transitions

from app.services.jira import JiraClient

def test_manual_ticket_transition():
    # Initialize client 
    client = JiraClient()
    
    # Specify manually created ticket here
    target_ticket = "DFLW-48" 
    target_status = "Done"
    
    print(f"Initializing status transition test for ticket: {target_ticket}")
    
    # Invoke the function by passing the key and target status
    success = client.transition_issue(issue_key=target_ticket, target_status_name=target_status)
    
    if success:
        print("Test passed! Go check your Jira board.")
    else:
        print("Test failed.")

if __name__ == "__main__":
    test_manual_ticket_transition()