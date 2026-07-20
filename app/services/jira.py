import requests
from requests.auth import HTTPBasicAuth
from typing import List, Dict, Any, Optional
from app.core.config import settings

class JiraClient:
    def __init__(self):
        settings.validate_jira()
        self.auth = HTTPBasicAuth(settings.JIRA_USER_EMAIL, settings.JIRA_API_TOKEN)
        self.headers = {"Accept": "application/json"}
        self.base_url = settings.JIRA_HOST.rstrip("/")

    def _normalize_issue(self, jira_issue: Dict[str, Any]) -> Dict[str, Any]:
        """
        Transforms raw Jira API responses into the stable, normalized 
        Symphony Core Domain Model.
        """
        fields = jira_issue.get("fields", {})
        labels = [label.strip().lower() for label in fields.get("labels", [])]
        
        return {
            "id": jira_issue.get("id"),
            "identifier": jira_issue.get("key"),  # e.g., "SJB-101"
            "title": fields.get("summary"),
            "description": fields.get("description"),
            "priority": int(fields.get("priority", {}).get("id", 3)) if fields.get("priority") else None,
            "state": fields.get("status", {}).get("name", "Unknown").lower(),
            "branch_name": f"symphony/{jira_issue.get('key')}",
            "url": f"{self.base_url}/browse/{jira_issue.get('key')}",
            "labels": labels,
            "blocked_by": [],
            "created_at": fields.get("created"),
            "updated_at": fields.get("updated")
        }

    def fetch_candidate_issues(self, active_states: List[str]) -> List[Dict[str, Any]]:
        """Spec 11.1.1: Return issues in configured active states using the modern /jql endpoint"""
        # Escape statuses with single quotes
        states_jql = ", ".join([f"'{state}'" for state in active_states])
        jql = f"project = '{settings.JIRA_PROJECT_KEY}' AND status IN ({states_jql}) ORDER BY priority ASC, created ASC"
        
        # Appended /jql to migrate to the mandatory Atlassian endpoint
        url = f"{self.base_url}/rest/api/3/search/jql"
        params = {"jql": jql, "maxResults": 50}
        
        response = requests.get(url, headers=self.headers, auth=self.auth, params=params)
        if response.status_code != 200:
            print(f"Jira API Candidate Fetch Error: {response.status_code} - {response.text}")
            return []
            
        issues = response.json().get("issues", [])
        return [self._normalize_issue(i) for i in issues]

    def fetch_issue_states_by_ids(self, issue_ids: List[str]) -> List[Dict[str, Any]]:
        """Spec 11.1.3: Active-run reconciliation loops utilizing the /jql endpoint"""
        if not issue_ids:
            return []
            
        ids_jql = ", ".join([f"'{id_}'" for id_ in issue_ids])
        jql = f"id IN ({ids_jql})"
        
        # Appended /jql to prevent 410 Gone errors
        url = f"{self.base_url}/rest/api/3/search/jql"
        params = {"jql": jql, "fields": "status,summary,description,priority,labels,created,updated"}
        
        response = requests.get(url, headers=self.headers, auth=self.auth, params=params)
        if response.status_code != 200:
            return []
            
        issues = response.json().get("issues", [])
        return [self._normalize_issue(i) for i in issues]

    def transition_issue(self, issue_key: str, target_status_name: str) -> bool:
        """
        Transitions a Jira issue to a target status by looking up its available transitions.
        """
        # Reuse self.headers but ensure Content-Type is added for the upcoming POST request
        headers = self.headers.copy()
        headers["Content-Type"] = "application/json"
        
        # Use self.base_url and self.auth which are already configured in __init__
        transitions_url = f"{self.base_url}/rest/api/3/issue/{issue_key}/transitions"
        
        response = requests.get(transitions_url, headers=headers, auth=self.auth)
        if response.status_code != 200:
            print(f"Failed to fetch transitions for {issue_key}: {response.text}")
            return False
            
        transitions = response.json().get("transitions", [])
        transition_id = None
        
        # Find the transition ID that matches our target status name (case-insensitive)
        for t in transitions:
            if t.get("to", {}).get("name", "").lower() == target_status_name.lower():
                transition_id = t.get("id")
                break
                
        if not transition_id:
            available = [t.get("to", {}).get("name") for t in transitions]
            print(f"No valid transition found to '{target_status_name}' for {issue_key}. Available states: {available}")
            return False
            
        # Execute the transition
        payload = {"transition": {"id": transition_id}}
        
        print(f"Transitioning {issue_key} to '{target_status_name}' (ID: {transition_id})...")
        post_response = requests.post(transitions_url, headers=headers, auth=self.auth, json=payload)
        
        if post_response.status_code == 204:
            print(f"Successfully updated {issue_key} to '{target_status_name}'!")
            return True
        else:
            print(f"Transition failed: {post_response.status_code} - {post_response.text}")
            return False
