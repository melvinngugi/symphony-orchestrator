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
        
        # FIXED: Appended /jql to migrate to the mandatory Atlassian endpoint
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
        
        # FIXED: Appended /jql to prevent 410 Gone errors
        url = f"{self.base_url}/rest/api/3/search/jql"
        params = {"jql": jql, "fields": "status,summary,description,priority,labels,created,updated"}
        
        response = requests.get(url, headers=self.headers, auth=self.auth, params=params)
        if response.status_code != 200:
            return []
            
        issues = response.json().get("issues", [])
        return [self._normalize_issue(i) for i in issues]
