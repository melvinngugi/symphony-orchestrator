from app.services.bitbucket import bitbucket_service

def diagnose_bitbucket():
    print("Running Symphony Bitbucket Diagnostics...")
    try:
        repo_data = bitbucket_service.verify_repository()
        repo_name = repo_data.get("full_name")
        is_private = repo_data.get("is_private")
        default_branch = repo_data.get("mainbranch", {}).get("name", "Not set (Empty Repo)")

        print("Successfully connected to Bitbucket REST API!")
        print(f"Repository: {repo_name}")
        print(f"Private:    {is_private}")
        print(f"Main Branch: {default_branch}")

    except Exception as e:
        print(f"Bitbucket connection failed: {e}")

if __name__ == "__main__":
    diagnose_bitbucket()