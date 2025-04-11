import os
import requests
import re
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GITHUB_API_URL = os.getenv("GITHUB_API_URL", "https://api.github.com")
GITHUB_TOKEN = os.getenv("INPUT_GITHUB_TOKEN") # Input from action.yml
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY") # e.g., owner/repo
GITHUB_EVENT_PATH = os.getenv("GITHUB_EVENT_PATH") # Path to the event payload JSON

if not GITHUB_TOKEN:
    logger.warning("GITHUB_TOKEN not found in environment variables.")
if not GITHUB_REPOSITORY:
    logger.error("GITHUB_REPOSITORY environment variable not set.")
    exit(1) # Critical failure
if not GITHUB_EVENT_PATH or not os.path.exists(GITHUB_EVENT_PATH):
    logger.error(f"GITHUB_EVENT_PATH {GITHUB_EVENT_PATH} not found or invalid.")
    exit(1) # Critical failure

OWNER, REPO = GITHUB_REPOSITORY.split('/')

def get_github_headers():
    """Returns headers required for GitHub API requests."""
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def get_pr_details_from_event():
    """
    Reads the event payload to get PR number and other relevant details.
    Returns the PR number or None if not found.
    """
    import json
    try:
        with open(GITHUB_EVENT_PATH, 'r') as f:
            event_payload = json.load(f)
        # Check for pull_request key, common in pull_request events
        if 'pull_request' in event_payload and 'number' in event_payload['pull_request']:
            return event_payload['pull_request']['number']
        # Check for number key directly, common in issue_comment events on PRs
        elif 'issue' in event_payload and 'pull_request' in event_payload['issue'] and 'number' in event_payload:
             return event_payload['number'] # Sometimes the top-level number is the PR number
        elif 'number' in event_payload: # Fallback for other potential event types
             return event_payload['number']
        else:
            logger.error("Could not find pull request number in event payload.")
            logger.debug(f"Event Payload: {event_payload}")
            return None
    except json.JSONDecodeError:
        logger.error(f"Failed to decode JSON from {GITHUB_EVENT_PATH}")
        return None
    except Exception as e:
        logger.error(f"Error reading event payload: {e}")
        return None


def get_pr_diff(pr_number):
    """
    Fetches the diff for a given pull request number.
    Returns the diff content as a string or None on failure.
    """
    if not pr_number:
        logger.error("PR number is required to fetch diff.")
        return None
    diff_url = f"{GITHUB_API_URL}/repos/{OWNER}/{REPO}/pulls/{pr_number}"
    headers = get_github_headers()
    # Request diff format
    headers["Accept"] = "application/vnd.github.v3.diff"
    try:
        response = requests.get(diff_url, headers=headers)
        response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
        logger.info(f"Successfully fetched diff for PR #{pr_number}")
        return response.text
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching PR diff for PR #{pr_number}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response status: {e.response.status_code}")
            logger.error(f"Response text: {e.response.text}")
        return None

def find_linked_issue_number(pr_number):
    """
    Finds the issue number linked to the PR.
    Searches the PR body for keywords like 'Closes #', 'Fixes #', etc.
    Alternatively, could use GitHub's linked issues API if available/preferred.
    Returns the issue number (int) or None if not found.
    """
    if not pr_number:
        logger.error("PR number is required to find linked issue.")
        return None

    pr_url = f"{GITHUB_API_URL}/repos/{OWNER}/{REPO}/pulls/{pr_number}"
    headers = get_github_headers()
    try:
        response = requests.get(pr_url, headers=headers)
        response.raise_for_status()
        pr_data = response.json()
        pr_body = pr_data.get("body", "")

        if not pr_body:
            logger.warning(f"PR #{pr_number} body is empty. Cannot find linked issue via body text.")
            # TODO: Optionally add logic here to check GitHub's linked issues API
            return None

        # Regex to find common closing keywords followed by an issue number
        # Handles variations like Closes #123, fixes # 123, resolves: #123 etc.
        patterns = [
            r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)[\s:]*#(\d+)"
        ]
        for pattern in patterns:
            match = re.search(pattern, pr_body, re.IGNORECASE)
            if match:
                issue_number = int(match.group(1))
                logger.info(f"Found linked issue #{issue_number} in PR #{pr_number} body.")
                return issue_number

        logger.warning(f"Could not find linked issue number in PR #{pr_number} body.")
        # TODO: Optionally add logic here to check GitHub's linked issues API
        return None

    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching PR details for PR #{pr_number}: {e}")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred while finding linked issue for PR #{pr_number}: {e}")
        return None


def get_issue_body(issue_number):
    """
    Fetches the body content of a given issue number.
    Returns the issue body as a string or None on failure.
    """
    if not issue_number:
        logger.error("Issue number is required to fetch body.")
        return None
    issue_url = f"{GITHUB_API_URL}/repos/{OWNER}/{REPO}/issues/{issue_number}"
    headers = get_github_headers()
    try:
        response = requests.get(issue_url, headers=headers)
        response.raise_for_status()
        issue_data = response.json()
        logger.info(f"Successfully fetched body for issue #{issue_number}")
        return issue_data.get("body", "") # Return empty string if body is null/missing
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching issue body for issue #{issue_number}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response status: {e.response.status_code}")
            logger.error(f"Response text: {e.response.text}")
        return None

def post_pr_comment(pr_number, comment_body):
    """
    Posts a comment to the specified pull request.
    """
    if not pr_number:
        logger.error("PR number is required to post comment.")
        return False
    if not comment_body:
        logger.warning("Comment body is empty, not posting.")
        return False

    comment_url = f"{GITHUB_API_URL}/repos/{OWNER}/{REPO}/issues/{pr_number}/comments"
    headers = get_github_headers()
    payload = {"body": comment_body}
    try:
        response = requests.post(comment_url, headers=headers, json=payload)
        response.raise_for_status()
        logger.info(f"Successfully posted comment to PR #{pr_number}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Error posting comment to PR #{pr_number}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response status: {e.response.status_code}")
            logger.error(f"Response text: {e.response.text}")
        return False
