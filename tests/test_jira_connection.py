# To verify the Jira API tracking and connection infrastructure, run the integrated diagnostic script.

import json
import requests
from requests.auth import HTTPBasicAuth
from app.core.config import settings

def diagnose_jira_backlog():
    print("Running Symphony Jira Diagnostics...")
    settings.validate_jira()
    
    auth = HTTPBasicAuth(settings.JIRA_USER_EMAIL, settings.JIRA_API_TOKEN)
    url = f"{settings.JIRA_HOST.rstrip('/')}/rest/api/3/search/jql"
    
    jql = f"project = '{settings.JIRA_PROJECT_KEY}' ORDER BY created DESC"
    params = {"jql": jql, "maxResults": 10, "fields": "status,summary"}
    
    response = requests.get(url, headers={"Accept": "application/json"}, auth=auth, params=params)
    
    if response.status_code != 200:
        print(f"Error fetching diagnostics: {response.status_code} - {response.text}")
        return

    issues = response.json().get("issues", [])
    if not issues:
        print(f"Zero issues found in project '{settings.JIRA_PROJECT_KEY}'.")
        return

    print(f"Found {len(issues)} total issues! Here are their exact workflow names:\n")
    print(f"{'Issue Key':<12} | {'Summary':<45} | {'Internal Status Name'}")
    print("-" * 85)
    for issue in issues:
        key = issue.get("key")
        summary = issue.get("fields", {}).get("summary", "")[:43]
        status = issue.get("fields", {}).get("status", {}).get("name")
        print(f"{key:<12} | {summary:<45} | {status}")

if __name__ == "__main__":
    diagnose_jira_backlog()
