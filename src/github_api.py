import os
import json
import logging
import base64
import re
import requests
from typing import Optional, Dict, Any, Tuple

from github import Github, GithubException, PullRequest, Issue, ContentFile
from github.GithubException import UnknownObjectException

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Configuration ---
GITHUB_API_URL: Optional[str] = os.getenv("GITHUB_API_URL", "https://api.github.com") # PyGithub uses base_url
GITHUB_TOKEN: Optional[str] = os.getenv("INPUT_GITHUB_TOKEN") # Input from action.yml
GITHUB_REPOSITORY: Optional[str] = os.getenv("GITHUB_REPOSITORY") # e.g., owner/repo
GITHUB_EVENT_PATH: Optional[str] = os.getenv("GITHUB_EVENT_PATH") # Path to the event payload JSON

# --- Initialization and Validation ---
if not GITHUB_TOKEN:
    logger.warning("GITHUB_TOKEN not found in environment variables. API access will be limited.")
if not GITHUB_REPOSITORY:
    logger.error("GITHUB_REPOSITORY environment variable not set.")
    exit(1)
if not GITHUB_EVENT_PATH or not os.path.exists(GITHUB_EVENT_PATH):
    logger.error(f"GITHUB_EVENT_PATH '{GITHUB_EVENT_PATH}' not found or invalid.")
    exit(1)

try:
    OWNER, REPO = GITHUB_REPOSITORY.split('/')
except ValueError:
    logger.error(f"Invalid GITHUB_REPOSITORY format: {GITHUB_REPOSITORY}. Expected 'owner/repo'.")
    exit(1)

github_client: Github = Github(
    base_url=GITHUB_API_URL,
    login_or_token=GITHUB_TOKEN
) if GITHUB_API_URL != "https://api.github.com" else Github(login_or_token=GITHUB_TOKEN)

try:
    repo = github_client.get_repo(f"{OWNER}/{REPO}")
    logger.info(f"Successfully connected to repository: {OWNER}/{REPO}")
except UnknownObjectException:
    logger.error(f"Repository {OWNER}/{REPO} not found or token lacks permissions.")
    exit(1)
except GithubException as e:
    logger.error(f"Error connecting to GitHub: {e.status} {e.data}")
    exit(1)

# --- Helper Functions ---

def get_pr_number_from_event() -> Optional[int]:
    """Reads the event payload to get PR number."""
    try:
        with open(GITHUB_EVENT_PATH, 'r') as f:
            event_payload: Dict[str, Any] = json.load(f)

        if 'pull_request' in event_payload and 'number' in event_payload['pull_request']:
            return int(event_payload['pull_request']['number'])
        if 'issue' in event_payload and 'number' in event_payload['issue']:
             issue_url = event_payload['issue'].get('url', '')
             if '/pulls/' in issue_url:
                 return int(event_payload['issue']['number'])
             else:
                 logger.info("Event is an issue comment, not a pull request comment.")
                 return None
        if 'number' in event_payload:
             logger.warning(f"Found 'number' ({event_payload['number']}) directly in payload, assuming it's PR number.")
             return int(event_payload['number'])

        logger.error("Could not reliably determine pull request number from event payload.")
        return None
    except json.JSONDecodeError:
        logger.error(f"Failed to decode JSON from {GITHUB_EVENT_PATH}")
        return None
    except KeyError as e:
        logger.error(f"Missing expected key in event payload: {e}")
        return None
    except Exception as e:
        logger.error(f"Error reading event payload: {e}")
        return None

def get_pull_request(pr_number: int) -> Optional[PullRequest.PullRequest]:
    """Gets the PyGithub PullRequest object."""
    try:
        pr = repo.get_pull(pr_number)
        logger.info(f"Successfully retrieved PR object for #{pr_number}")
        return pr
    except UnknownObjectException:
        logger.error(f"Pull Request #{pr_number} not found in {OWNER}/{REPO}.")
        return None
    except GithubException as e:
        logger.error(f"Error getting PR #{pr_number}: {e.status} {e.data}")
        return None

def get_issue(issue_number: int) -> Optional[Issue.Issue]:
    """Gets the PyGithub Issue object."""
    try:
        issue = repo.get_issue(issue_number)
        logger.info(f"Successfully retrieved Issue object for #{issue_number}")
        return issue
    except UnknownObjectException:
        logger.error(f"Issue #{issue_number} not found in {OWNER}/{REPO}.")
        return None
    except GithubException as e:
        logger.error(f"Error getting Issue #{issue_number}: {e.status} {e.data}")
        return None

# --- Core API Functions ---

def get_pr_diff(pr: PullRequest.PullRequest) -> Optional[str]:
    """Fetches the diff for a given PyGithub PullRequest object."""
    if not pr:
        logger.error("Valid PullRequest object is required to fetch diff.")
        return None
    try:
        diff_url = pr.url
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3.diff",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        response = requests.get(diff_url, headers=headers)
        response.raise_for_status()
        logger.info(f"Successfully fetched diff for PR #{pr.number}")
        return response.text
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching PR diff for PR #{pr.number} via requests: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response status: {e.response.status_code}, Text: {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred fetching diff for PR #{pr.number}: {e}")
        return None

def _find_issue_via_regex(pr: PullRequest.PullRequest) -> Optional[int]:
    """Helper function to find linked issue number using regex on PR body."""
    pr_body = pr.body
    if not pr_body:
        return None

    # Use re imported at top level
    patterns = [
        r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)[\s:]*#(\d+)"
    ]
    for pattern in patterns:
        match = re.search(pattern, pr_body, re.IGNORECASE)
        if match:
            issue_number = int(match.group(1))
            logger.info(f"Found potential linked issue #{issue_number} via regex fallback in PR #{pr.number} body.")
            return issue_number

    return None

def find_linked_issue_number(pr: PullRequest.PullRequest) -> Optional[int]:
    """
    Finds the *first* issue explicitly linked to the PR, prioritizing timeline events
    and falling back to a regex search on the PR body.
    """
    if not pr:
        logger.error("Valid PullRequest object is required to find linked issue.")
        return None

    try:
        # --- Strategy 1: Check Timeline Events (Most Reliable) ---
        timeline = None
        try:
            timeline = pr.get_issue_events()
        except Exception as e_fetch:
            logger.error(f"Error occurred *during* fetch of timeline events for PR #{pr.number}: {e_fetch}", exc_info=True)
            timeline = None # Ensure timeline is None if fetch failed

        if timeline:
            event_count = 0
            for event in timeline:
                event_count += 1
                if event.event == 'cross-referenced' and event.source and event.source.issue:
                    source_issue = event.source.issue
                    if source_issue.number != pr.number and source_issue.repository.full_name == repo.full_name:
                        linked_issue_number = source_issue.number
                        logger.info(f"Found linked issue #{linked_issue_number} via '{event.event}' event for PR #{pr.number}.")
                        return linked_issue_number
            logger.info(f"Processed {event_count} timeline events for PR #{pr.number}. No explicitly linked issue event found.")

        # --- Strategy 2: Fallback to Regex on PR Body ---
        logger.info(f"Falling back to regex check on PR #{pr.number} body.")
        linked_issue_number = _find_issue_via_regex(pr)
        if linked_issue_number:
            return linked_issue_number
        else:
            logger.warning(f"Could not find linked issue number via timeline or regex fallback for PR #{pr.number}.")
            return None

    except GithubException as e_outer:
        logger.error(f"GitHub API error during linked issue search for PR #{pr.number}: {e_outer.status} {e_outer.data}", exc_info=True)
        return None
    except Exception as e_outer:
        logger.error(f"An unexpected error occurred while finding linked issue for PR #{pr.number}: {e_outer}", exc_info=True)
        return None

def get_file_content(pr: PullRequest.PullRequest, file_path: str) -> Optional[str]:
    """Gets the content of a specific file at the PR's head commit."""
    if not pr:
        logger.error("Valid PullRequest object is required to fetch file content.")
        return None
    if not file_path:
        logger.error("File path is required.")
        return None

    try:
        content_file: ContentFile = repo.get_contents(file_path, ref=pr.head.sha)

        if isinstance(content_file, list):
             logger.error(f"Path '{file_path}' refers to a directory, not a file.")
             return None
        if content_file.type != 'file':
            logger.error(f"Path '{file_path}' is not a file (type: {content_file.type}).")
            return None

        if content_file.content:
            decoded_content = base64.b64decode(content_file.content).decode('utf-8')
            logger.info(f"Successfully retrieved and decoded content for '{file_path}'.")
            return decoded_content
        else:
            logger.info(f"File '{file_path}' is empty.")
            return ""

    except UnknownObjectException:
        logger.error(f"File '{file_path}' not found in PR #{pr.number} head ({pr.head.sha}).")
        return None
    except GithubException as e:
        logger.error(f"GitHub API error getting content for '{file_path}': {e.status} {e.data}")
        return None
    except UnicodeDecodeError as e:
        logger.error(f"Error decoding content (not valid UTF-8?) for '{file_path}': {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error processing content for '{file_path}': {e}", exc_info=True)
        return None

def get_issue_body(issue: Issue.Issue) -> Optional[str]:
    """Gets the body content of a given PyGithub Issue object."""
    if not issue:
        logger.error("Valid Issue object is required to fetch body.")
        return None
    try:
        body = issue.body if issue.body is not None else ""
        logger.info(f"Successfully retrieved body for issue #{issue.number}")
        return body
    except Exception as e:
        logger.error(f"An unexpected error occurred getting body for issue #{issue.number}: {e}")
        return None

def post_pr_comment(pr_or_issue_number: int, comment_body: str) -> bool:
    """Posts a comment to the specified pull request or issue number."""
    if not pr_or_issue_number:
        logger.error("PR or Issue number is required to post comment.")
        return False
    if not comment_body:
        logger.warning("Comment body is empty, not posting.")
        return False

    try:
        target_issue = repo.get_issue(pr_or_issue_number)
        target_issue.create_comment(comment_body)
        logger.info(f"Successfully posted comment to Issue/PR #{pr_or_issue_number}")
        return True
    except UnknownObjectException:
        logger.error(f"Issue/PR #{pr_or_issue_number} not found for posting comment.")
        return False
    except GithubException as e:
        logger.error(f"Error posting comment to Issue/PR #{pr_or_issue_number}: {e.status} {e.data}")
        return False
    except Exception as e:
        logger.error(f"An unexpected error occurred posting comment to #{pr_or_issue_number}: {e}")
        return False
