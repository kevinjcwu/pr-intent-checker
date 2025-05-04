import os
import json
import logging
import base64 # Needed for decoding file content
from typing import Optional, Dict, Any, Tuple

from github import Github, GithubException, PullRequest, Issue, ContentFile
from github.GithubException import UnknownObjectException

# Force DEBUG level for action logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Configuration ---
GITHUB_API_URL: Optional[str] = os.getenv("GITHUB_API_URL", "https://api.github.com") # PyGithub uses base_url
GITHUB_TOKEN: Optional[str] = os.getenv("INPUT_GITHUB_TOKEN") # Input from action.yml
GITHUB_REPOSITORY: Optional[str] = os.getenv("GITHUB_REPOSITORY") # e.g., owner/repo
GITHUB_EVENT_PATH: Optional[str] = os.getenv("GITHUB_EVENT_PATH") # Path to the event payload JSON

# --- Initialization and Validation ---
if not GITHUB_TOKEN:
    # Changed to warning as PyGithub might allow unauthenticated access for some public data
    logger.warning("GITHUB_TOKEN not found in environment variables. API access will be limited.")
    # Depending on required operations, might need to exit(1) if token is essential

if not GITHUB_REPOSITORY:
    logger.error("GITHUB_REPOSITORY environment variable not set.")
    exit(1) # Critical failure

if not GITHUB_EVENT_PATH or not os.path.exists(GITHUB_EVENT_PATH):
    logger.error(f"GITHUB_EVENT_PATH '{GITHUB_EVENT_PATH}' not found or invalid.")
    exit(1) # Critical failure

try:
    OWNER, REPO = GITHUB_REPOSITORY.split('/')
except ValueError:
    logger.error(f"Invalid GITHUB_REPOSITORY format: {GITHUB_REPOSITORY}. Expected 'owner/repo'.")
    exit(1)

# Initialize PyGithub client
# Use enterprise URL if GITHUB_API_URL is different from default
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
    """
    Reads the event payload to get PR number.
    Handles common event types like pull_request and issue_comment on a PR.
    """
    try:
        with open(GITHUB_EVENT_PATH, 'r') as f:
            event_payload: Dict[str, Any] = json.load(f)

        # Check for pull_request event
        if 'pull_request' in event_payload and 'number' in event_payload['pull_request']:
            return int(event_payload['pull_request']['number'])

        # Check for issue_comment event on a PR
        # Note: issue_comment events have an 'issue' object which might have a 'pull_request' key
        if 'issue' in event_payload and 'number' in event_payload['issue']:
             # Check if the issue is actually a pull request
             issue_url = event_payload['issue'].get('url', '')
             # A simple check, might need refinement based on exact event payloads
             if '/pulls/' in issue_url:
                 return int(event_payload['issue']['number'])
             else:
                 # If it's an issue comment but not on a PR context we care about
                 logger.info("Event is an issue comment, not a pull request comment.")
                 return None

        # Fallback for other potential event types where 'number' might be the PR number
        if 'number' in event_payload:
             # This is less reliable, might need context check
             logger.warning(f"Found 'number' ({event_payload['number']}) directly in payload, assuming it's PR number.")
             # Consider adding checks here if this path is hit unexpectedly
             return int(event_payload['number'])

        logger.error("Could not reliably determine pull request number from event payload.")
        logger.debug(f"Event Payload Keys: {event_payload.keys()}")
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
    """
    Fetches the diff for a given PyGithub PullRequest object.
    Returns the diff content as a string or None on failure.
    """
    if not pr:
        logger.error("Valid PullRequest object is required to fetch diff.")
        return None
    try:
        # PyGithub doesn't have a direct diff method, use requests with appropriate headers
        # Reusing the requests logic here as it's specific for the diff format
        diff_url = pr.url # Get the API URL from the PR object
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3.diff", # Request diff format
            "X-GitHub-Api-Version": "2022-11-28",
        }
        # Need to import requests here or make it a global import again
        import requests
        response = requests.get(diff_url, headers=headers)
        response.raise_for_status()
        logger.info(f"Successfully fetched diff for PR #{pr.number}")
        return response.text
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching PR diff for PR #{pr.number} via requests: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response status: {e.response.status_code}")
            logger.error(f"Response text: {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred fetching diff for PR #{pr.number}: {e}")
        return None


def find_linked_issue_number(pr: PullRequest.PullRequest) -> Optional[int]:
    """
    Finds the *first* issue explicitly linked to the PR using timeline events.
    Returns the issue number (int) or None if not found.
    """
    if not pr:
        logger.error("Valid PullRequest object is required to find linked issue.")
        return None

    try:
        # Iterate through the PR's timeline events to find 'cross-referenced' events
        # where the source is an issue. This is the most reliable way.
        timeline = None # Initialize timeline
        try:
            logger.debug(f"Attempting to fetch timeline events for PR #{pr.number} using get_issue_events()...")
            timeline = pr.get_issue_events() # Or pr.get_timeline() in newer PyGithub? Check docs.
            logger.debug(f"Successfully fetched timeline events object for PR #{pr.number}.")
        except Exception as e_fetch:
            logger.error(f"Error occurred *during* fetch of timeline events for PR #{pr.number}: {e_fetch}", exc_info=True)
            # Exit or return None if fetching fails critically
            return None

        if timeline is None:
             logger.error(f"Timeline events object is None after fetch attempt for PR #{pr.number}.")
             return None

        logger.debug(f"Checking timeline events iterator for PR #{pr.number}...") # Add debug log start
        found_link = False # Flag to track if we found the link
        event_count = 0 # Count events processed
        for event in timeline:
            event_count += 1 # Increment count
            logger.debug(f"Timeline event type: {event.event}") # Log the event type
            # Check for events indicating an issue was linked (e.g., 'connected', 'cross-referenced')
            # The exact event type/structure might need verification with GitHub API docs
            # Let's assume 'cross-referenced' is a key indicator for now.
            # We need the event where the *source* points to the issue we want.
            if event.event == 'cross-referenced' and event.source and event.source.issue:
                 # Check if the source issue is in the same repo and is not the PR itself
                 # (PRs are also issues, so a PR might reference itself)
                 source_issue = event.source.issue
                 if source_issue.number != pr.number and source_issue.repository.full_name == repo.full_name:
                     linked_issue_number = source_issue.number
                     logger.info(f"Found linked issue #{linked_issue_number} via '{event.event}' event for PR #{pr.number}.")
                     found_link = True # Set flag
                     return linked_issue_number # Return immediately

            # Alternative: Check for 'connected' event if 'cross-referenced' isn't right
            # You might add a similar check here if needed:
            # elif event.event == 'connected' and ... :
            #    # logic to extract issue number from connected event
            #    logger.info(f"Found linked issue via 'connected' event...")
            #    found_link = True
            #    return linked_issue_number

        # If loop completes without finding a linked issue via timeline events
        if not found_link:
            logger.warning(f"Processed {event_count} timeline events for PR #{pr.number}. Found no explicitly linked issue event. Falling back to regex check on PR body.")
            # --- Fallback: Regex check on PR body ---
            pr_body = pr.body
            if not pr_body:
                logger.warning(f"PR #{pr.number} body is empty. Cannot find linked issue via body text either.")
                return None

            import re # Import locally if only used here
            patterns = [
                r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)[\s:]*#(\d+)"
            ]
            for pattern in patterns:
                match = re.search(pattern, pr_body, re.IGNORECASE)
                if match:
                    issue_number = int(match.group(1))
                    logger.info(f"Found potential linked issue #{issue_number} via regex fallback in PR #{pr.number} body.")
                    return issue_number

            logger.warning(f"Could not find linked issue number via regex fallback in PR #{pr.number} body.")
            return None # Return None if fallback also fails
        else:
             # This case should not be reached if found_link is True, as we return earlier
             return None


    except GithubException as e_outer:
        logger.error(f"GitHub API error finding linked issue for PR #{pr.number}: {e_outer.status} {e_outer.data}", exc_info=True)
        return None
    except Exception as e_outer:
        logger.error(f"An unexpected error occurred while finding linked issue for PR #{pr.number}: {e_outer}", exc_info=True)
        return None


def get_file_content(pr: PullRequest.PullRequest, file_path: str) -> Optional[str]:
    """
    Gets the content of a specific file at the PR's head commit.

    Args:
        pr: The PyGithub PullRequest object.
        file_path: The path to the file within the repository.

    Returns:
        The decoded file content as a string, or None on failure.
    """
    if not pr:
        logger.error("Valid PullRequest object is required to fetch file content.")
        return None
    if not file_path:
        logger.error("File path is required.")
        return None

    try:
        # Get the content object using the PR's head SHA as the reference
        content_file: ContentFile = repo.get_contents(file_path, ref=pr.head.sha)

        # Check if it's actually a file (not a directory)
        if isinstance(content_file, list):
             logger.error(f"Path '{file_path}' refers to a directory, not a file, in PR #{pr.number} head ({pr.head.sha}).")
             return None
        if content_file.type != 'file':
            logger.error(f"Path '{file_path}' is not a file (type: {content_file.type}) in PR #{pr.number} head ({pr.head.sha}).")
            return None

        # Content is base64 encoded, decode it
        if content_file.content:
            decoded_content = base64.b64decode(content_file.content).decode('utf-8')
            logger.info(f"Successfully retrieved and decoded content for '{file_path}' from PR #{pr.number} head ({pr.head.sha}).")
            return decoded_content
        else:
            # Handle case of empty file
            logger.info(f"File '{file_path}' from PR #{pr.number} head ({pr.head.sha}) is empty.")
            return ""

    except UnknownObjectException:
        logger.error(f"File '{file_path}' not found in PR #{pr.number} head ({pr.head.sha}).")
        return None
    except GithubException as e:
        logger.error(f"GitHub API error getting content for '{file_path}' in PR #{pr.number} head ({pr.head.sha}): {e.status} {e.data}")
        return None
    except Exception as e:
        # Catch potential decoding errors or other issues
        logger.error(f"Error processing content for '{file_path}' in PR #{pr.number} head ({pr.head.sha}): {e}")
        return None


def get_issue_body(issue: Issue.Issue) -> Optional[str]:
    """
    Gets the body content of a given PyGithub Issue object.
    Returns the issue body as a string or None on failure (e.g., null body).
    """
    if not issue:
        logger.error("Valid Issue object is required to fetch body.")
        return None
    try:
        # Body can be None, return empty string in that case
        body = issue.body if issue.body is not None else ""
        logger.info(f"Successfully retrieved body for issue #{issue.number}")
        return body
    except Exception as e:
        # Unlikely to fail here if issue object is valid, but just in case
        logger.error(f"An unexpected error occurred getting body for issue #{issue.number}: {e}")
        return None


def post_pr_comment(pr_or_issue_number: int, comment_body: str) -> bool:
    """
    Posts a comment to the specified pull request or issue number.
    """
    if not pr_or_issue_number:
        logger.error("PR or Issue number is required to post comment.")
        return False
    if not comment_body:
        logger.warning("Comment body is empty, not posting.")
        # Consider returning True here as it's not an error, just skipped.
        # Let's return False for now to indicate no comment was posted.
        return False

    try:
        # PRs are issues, so we can use get_issue to post comments
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
