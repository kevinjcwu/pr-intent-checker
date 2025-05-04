import os
import json
import logging
import ast  # For parsing Python code
import inspect # For getting source code from nodes (fallback)
import re # For parsing diff hunks
from typing import Optional, Dict, Any, Tuple, List, Set

from github import Github, GithubException, PullRequest, Issue, ContentFile, GitCommit
from github.GithubException import UnknownObjectException
from github.File import File as GithubFile # Rename to avoid conflict

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

# --- AST Parsing Helpers ---

class FunctionInfo:
    """Stores information about a function definition."""
    def __init__(self, node: ast.FunctionDef):
        self.node = node
        self.name = node.name
        self.start_line = node.lineno
        self.end_line = node.end_lineno
        self.calls: List[str] = [] # Names of functions/methods called
        self.signature: str = ""
        self.body_source: str = "" # Full source code

class ClassInfo:
    """Stores information about a class definition."""
    def __init__(self, node: ast.ClassDef):
        self.node = node
        self.name = node.name
        self.start_line = node.lineno
        self.end_line = node.end_lineno
        self.methods: Dict[str, FunctionInfo] = {} # Method name -> FunctionInfo

class AstParser(ast.NodeVisitor):
    """
    Parses Python code using AST to extract imports, classes, functions,
    their line ranges, and calls made within functions.
    """
    def __init__(self, source_code: str):
        self.source_lines = source_code.splitlines()
        self.imports: List[str] = []
        self.functions: Dict[str, FunctionInfo] = {}
        self.classes: Dict[str, ClassInfo] = {}
        self._current_class_name: Optional[str] = None

    def _get_node_source(self, node):
        try:
            # ast.unparse is preferred (Python 3.9+)
            return ast.unparse(node)
        except AttributeError:
            # Fallback using inspect (less reliable for exact formatting)
            try:
                return inspect.getsource(node) # This might not work directly on AST nodes
            except:
                 # Manual slicing as a last resort (might be inaccurate)
                 start = node.lineno - 1
                 end = node.end_lineno
                 return "\n".join(self.source_lines[start:end])

    def _get_signature_source(self, node: ast.FunctionDef):
         # Extract source lines just for the signature part
         start = node.lineno -1
         end_sig_line = node.body[0].lineno - 1 if node.body else node.end_lineno
         # Adjust end line if decorators are present
         if node.decorator_list:
             start = node.decorator_list[0].lineno -1

         signature_lines = self.source_lines[start:end_sig_line]
         # Find the line containing 'def' and trim leading whitespace/decorators
         def_line_index = -1
         for i, line in enumerate(signature_lines):
             if line.strip().startswith("def "):
                 def_line_index = i
                 break
         
         if def_line_index != -1:
             # Get the indentation of the 'def' line
             indent = len(signature_lines[def_line_index]) - len(signature_lines[def_line_index].lstrip())
             # Include decorators if present
             start_index = 0
             # Reconstruct signature, preserving relative indentation
             sig = "\n".join(line[indent:] for line in signature_lines[start_index:])
             return sig.strip() # Return the cleaned signature
         else:
             # Fallback if 'def' not found as expected
             return f"def {node.name}(...): # Signature extraction failed"


    def visit_Import(self, node):
        self.imports.append(self._get_node_source(node))
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        self.imports.append(self._get_node_source(node))
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        class_info = ClassInfo(node)
        self.classes[node.name] = class_info
        self._current_class_name = node.name
        self.generic_visit(node) # Visit methods inside the class
        self._current_class_name = None

    def visit_FunctionDef(self, node):
        func_info = FunctionInfo(node)
        func_info.signature = self._get_signature_source(node)
        func_info.body_source = self._get_node_source(node)

        # Find calls within this function
        for body_node in ast.walk(node):
            if isinstance(body_node, ast.Call):
                call_name = ""
                if isinstance(body_node.func, ast.Name):
                    call_name = body_node.func.id
                elif isinstance(body_node.func, ast.Attribute):
                    try:
                        call_name = ast.unparse(body_node.func)
                    except AttributeError: # Fallback
                         if isinstance(body_node.func.value, ast.Name):
                             call_name = f"{body_node.func.value.id}.{body_node.func.attr}"
                         else:
                             call_name = f"?.{body_node.func.attr}"
                if call_name:
                    func_info.calls.append(call_name)

        if self._current_class_name:
            # This is a method within a class
            if self._current_class_name in self.classes:
                self.classes[self._current_class_name].methods[node.name] = func_info
        else:
            # This is a standalone function
            self.functions[node.name] = func_info
        # Don't call generic_visit here as we manually walked the body for calls


def parse_diff_hunks(diff_text: str) -> Dict[str, Set[int]]:
    """Parses a diff string and returns a dict mapping filename to changed line numbers."""
    changed_lines: Dict[str, Set[int]] = {}
    current_filename = None
    # Regex to find hunk headers like @@ -1,3 +1,4 @@
    hunk_header_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+)?)? @@")

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_filename = line[6:].strip()
            if current_filename not in changed_lines:
                changed_lines[current_filename] = set()
        elif line.startswith("@@") and current_filename:
            match = hunk_header_re.match(line)
            if match:
                start_line = int(match.group(1))
                count = int(match.group(2) or 1) # If count is omitted, it's 1 line
                current_line_in_hunk = start_line
                line_index_in_hunk = 0 # Track position within the hunk lines that follow
                # Pre-calculate target lines for this hunk
                target_lines = set(range(start_line, start_line + count)) if count > 0 else set()

        elif line.startswith("+") and not line.startswith("+++") and current_filename:
             # This is an added line, associate it with the current line number in the *new* file
             changed_lines[current_filename].add(current_line_in_hunk)
             current_line_in_hunk += 1
             line_index_in_hunk += 1
        elif line.startswith("-") and not line.startswith("---"):
             # This is a deleted line, it doesn't advance the line number in the new file
             # But we still need to advance our position within the hunk lines
             line_index_in_hunk += 1
             pass
        elif not line.startswith("@@") and current_filename:
             # This is an unchanged context line
             current_line_in_hunk += 1
             line_index_in_hunk += 1

    # Filter out files where only deletions occurred (no lines added/modified)
    return {f: lines for f, lines in changed_lines.items() if lines}


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


def get_contextual_code(pr: PullRequest.PullRequest, diff_text: str) -> str:
    """
    Fetches relevant code context for changes identified in a diff.
    Implements the refined strategy: full definition of changed blocks + signatures of local calls.

    Args:
        pr: The PyGithub PullRequest object.
        diff_text: The diff string for the PR.

    Returns:
        A string containing formatted contextual code snippets, or empty string if none found.
    """
    context_parts = []
    # Use the diff parser to find changed lines per file (in the new version)
    changed_py_files = parse_diff_hunks(diff_text)
    pr_head_sha = pr.head.sha # Get the commit SHA for the PR head

    logger.info(f"Found {len(changed_py_files)} Python files with changes in diff.")

    # Cache parsed file info to avoid redundant parsing if a file is processed multiple times
    # (e.g., if multiple changed functions call each other within the same file)
    parsed_file_cache: Dict[str, AstParser] = {}

    def get_parsed_file(filename: str) -> Optional[AstParser]:
        """Fetches file content and parses it, using a cache."""
        if filename in parsed_file_cache:
            return parsed_file_cache[filename]
        
        try:
            logger.debug(f"Fetching content for {filename} at ref {pr_head_sha}")
            content_item = repo.get_contents(filename, ref=pr_head_sha)
            if isinstance(content_item, list):
                 logger.warning(f"Path {filename} is a directory, skipping.")
                 return None
            if not isinstance(content_item, ContentFile) or content_item.type != 'file':
                 logger.warning(f"Path {filename} is not a file (type: {getattr(content_item, 'type', 'unknown')}), skipping.")
                 return None

            file_content = content_item.decoded_content.decode("utf-8")
            logger.debug(f"Successfully fetched content for {filename}")

            parser = AstParser(file_content)
            parser.visit(ast.parse(file_content))
            logger.debug(f"Parsed {filename}. Found {len(parser.functions)} funcs, {len(parser.classes)} classes.")
            parsed_file_cache[filename] = parser # Cache the result
            return parser

        except GithubException as e:
            if e.status == 403 and 'rate limit exceeded' in str(e.data).lower():
                 logger.error(f"Rate limit exceeded fetching content for {filename}. Skipping file.")
            elif e.status == 404:
                 logger.error(f"File not found: {filename} at commit {pr_head_sha}. Skipping.")
            else:
                 logger.error(f"GitHub API error fetching content for {filename}: {e.status} {e.data}")
            return None
        except SyntaxError as e:
            logger.error(f"Syntax error parsing {filename}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error processing file {filename}: {e}", exc_info=True)
            return None

    # --- Main Context Extraction Loop ---
    for filename, changed_lines in changed_py_files.items():
        if not filename.endswith(".py"):
            logger.debug(f"Skipping non-Python file: {filename}")
            continue

        parser = get_parsed_file(filename)
        if not parser:
            context_parts.append(f"\n--- Skipped File: {filename} (Could not fetch or parse) ---")
            continue

        # Find functions/methods containing changed lines in this file
        changed_blocks_in_file: List[Tuple[str, FunctionInfo, Optional[str]]] = [] # (type, info, class_name)
        for func_name, func_info in parser.functions.items():
            if changed_lines.intersection(range(func_info.start_line, func_info.end_line + 1)):
                changed_blocks_in_file.append(("Function", func_info, None))
        for class_name, class_info in parser.classes.items():
             for method_name, method_info in class_info.methods.items():
                 if changed_lines.intersection(range(method_info.start_line, method_info.end_line + 1)):
                     changed_blocks_in_file.append(("Method", method_info, class_name))

        if not changed_blocks_in_file:
            logger.debug(f"No function/method definitions found containing changed lines in {filename}")
            continue

        file_context_parts = [f"\n--- Context from File: {filename} ---"]
        processed_signatures_for_file = set() # Track signatures added for this file
        processed_imports_for_file = set() # Track imports added for this file

        # Extract context for each changed block found in this file
        for block_type, block_info, parent_class_name in changed_blocks_in_file:
            block_name = block_info.name
            block_display_name = f"{parent_class_name}.{block_name}" if parent_class_name else block_name

            file_context_parts.append(f"\n--- Context for Changed {block_type} `{block_display_name}` ---")
            file_context_parts.append("\nFull Definition:")
            file_context_parts.append(block_info.body_source)

            # Add signatures/imports for calls made *by* this block
            call_signatures = []
            relevant_imports = set()

            for call in block_info.calls:
                # Check if it's a local function/method in this file
                found_local = False
                local_sig = None
                # Check standalone functions first
                if call in parser.functions:
                    # Avoid adding signature for the block itself (recursion)
                    if call != block_name:
                         local_sig = parser.functions[call].signature
                         found_local = True
                else:
                    # Check methods in classes (simple check)
                    potential_method_name = call.split('.')[-1]
                    for c_info in parser.classes.values():
                        if potential_method_name in c_info.methods:
                            # Avoid adding signature for the block itself
                            if not (parent_class_name == c_info.name and potential_method_name == block_name):
                                local_sig = c_info.methods[potential_method_name].signature
                                found_local = True
                                break # Take first match

                if found_local and local_sig and call not in processed_signatures_for_file:
                    call_signatures.append(f"\nSignature of Local Function/Method Called `{call}`:")
                    call_signatures.append(local_sig)
                    processed_signatures_for_file.add(call)
                elif not found_local:
                    # Check if it relates to an import (simple check)
                    base_call = call.split('.')[0]
                    for imp in parser.imports:
                        # Basic check - might need refinement for complex import aliases/structures
                        if (f"import {base_call}" in imp or f"from {base_call} import" in imp or f"as {base_call}" in imp) and imp not in processed_imports_for_file:
                            relevant_imports.add(imp)
                            processed_imports_for_file.add(imp)
                            # No need to check other imports for this base_call once one is found
                            break 

            if call_signatures:
                file_context_parts.extend(call_signatures)
            if relevant_imports:
                file_context_parts.append("\nRelevant Imports:")
                file_context_parts.extend(sorted(list(relevant_imports)))

        context_parts.extend(file_context_parts)

    logger.info(f"Generated context for {len(context_parts)} parts.")
    return "\n".join(context_parts).strip()
