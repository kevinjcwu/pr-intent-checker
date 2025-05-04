import os
import json
import logging
import ast  # For parsing Python code
import inspect # For getting source code from nodes (fallback)
import re # For parsing diff hunks
import requests # For get_pr_diff
from typing import Optional, Dict, Any, Tuple, List, Set

# Correct PyGithub imports - Import classes directly
from github import Github, GithubException, UnknownObjectException, ContentFile, GitCommit 
from github.PullRequest import PullRequest # Import class directly
from github.Issue import Issue # Import class directly
# from github.File import File as GithubFile # Not used directly

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

# Initialize PyGithub client
github_client: Github = Github(
    base_url=GITHUB_API_URL,
    login_or_token=GITHUB_TOKEN
) if GITHUB_API_URL != "https://api.github.com" else Github(login_or_token=GITHUB_TOKEN)

try:
    # This repo object is used by PyGithub-based functions below
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
            return ast.unparse(node) # Requires Python 3.9+
        except AttributeError:
            try: # Fallback for older Python versions or complex nodes
                 start = node.lineno - 1
                 end = node.end_lineno
                 start = max(0, start)
                 end = min(len(self.source_lines), end)
                 return "\n".join(self.source_lines[start:end])
            except Exception:
                 logger.warning(f"Could not get source for node type {type(node)}", exc_info=True)
                 return f"# Error getting source for {type(node)}"

    def _get_signature_source(self, node: ast.FunctionDef):
         try:
             start = node.lineno -1
             end_sig_line = node.end_lineno
             if node.body:
                 first_body_node = node.body[0]
                 end_sig_line = first_body_node.lineno -1
                 if isinstance(first_body_node, ast.Expr) and isinstance(first_body_node.value, ast.Constant) and isinstance(first_body_node.value.value, str):
                      if len(node.body) > 1:
                           end_sig_line = node.body[1].lineno - 1
                      else:
                           end_sig_line = node.end_lineno
             if node.decorator_list:
                 start = node.decorator_list[0].lineno -1
             start = max(0, start)
             end_sig_line = max(start, end_sig_line)
             end_sig_line = min(len(self.source_lines), end_sig_line)
             signature_lines = self.source_lines[start:end_sig_line]
             def_line_index = -1
             for i, line in enumerate(signature_lines):
                 stripped_line = line.strip()
                 if stripped_line.startswith("def ") or stripped_line.startswith("async def "):
                     def_line_index = i
                     break
             if def_line_index != -1:
                 indent = len(signature_lines[def_line_index]) - len(signature_lines[def_line_index].lstrip())
                 start_index = 0 
                 sig = "\n".join(line[indent:] for line in signature_lines[start_index:])
                 if not sig.strip().endswith(':'): sig += ':'
                 return sig.strip()
             else:
                 return f"def {node.name}(...): # Signature extraction failed (def not found)"
         except Exception:
              logger.warning(f"Error extracting signature for {node.name}", exc_info=True)
              return f"def {node.name}(...): # Signature extraction error"

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
        self.generic_visit(node)
        self._current_class_name = None

    def visit_FunctionDef(self, node):
        func_info = FunctionInfo(node)
        func_info.signature = self._get_signature_source(node)
        func_info.body_source = self._get_node_source(node)
        for body_node in ast.walk(node):
            if isinstance(body_node, ast.Call):
                call_name = ""
                if isinstance(body_node.func, ast.Name):
                    call_name = body_node.func.id
                elif isinstance(body_node.func, ast.Attribute):
                    parts = []
                    curr = body_node.func
                    while isinstance(curr, ast.Attribute):
                        parts.append(curr.attr)
                        curr = curr.value
                    if isinstance(curr, ast.Name):
                        parts.append(curr.id)
                        call_name = ".".join(reversed(parts))
                    else:
                        call_name = f"?.{body_node.func.attr}" 
                if call_name:
                    func_info.calls.append(call_name)
        if self._current_class_name:
            if self._current_class_name in self.classes:
                self.classes[self._current_class_name].methods[node.name] = func_info
        else:
            self.functions[node.name] = func_info

def parse_diff_hunks(diff_text: str) -> Dict[str, Set[int]]:
    """Parses a diff string and returns a dict mapping filename to changed line numbers in the new file."""
    changed_lines: Dict[str, Set[int]] = {}
    current_filename = None
    hunk_header_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+)?)? @@")
    current_line_in_new_file = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_filename = line[6:].strip()
            if current_filename not in changed_lines: changed_lines[current_filename] = set()
        elif line.startswith("@@") and current_filename:
            match = hunk_header_re.match(line)
            if match: current_line_in_new_file = int(match.group(1))
            else: current_filename = None
        elif line.startswith("+") and not line.startswith("+++") and current_filename:
             changed_lines[current_filename].add(current_line_in_new_file)
             current_line_in_new_file += 1
        elif line.startswith("-") and not line.startswith("---"): pass
        elif not line.startswith("@@") and current_filename: current_line_in_new_file += 1
    return {f: lines for f, lines in changed_lines.items() if lines}

# --- Helper Functions (Original PyGithub Based) ---

def get_pr_number_from_event() -> Optional[int]:
    """Reads the event payload to get PR number."""
    try:
        with open(GITHUB_EVENT_PATH, 'r') as f: event_payload: Dict[str, Any] = json.load(f)
        if 'pull_request' in event_payload and 'number' in event_payload['pull_request']: return int(event_payload['pull_request']['number'])
        if 'issue' in event_payload and 'number' in event_payload['issue']:
             issue_url = event_payload['issue'].get('url', '')
             if '/pulls/' in issue_url: return int(event_payload['issue']['number'])
             else: logger.info("Event is an issue comment, not a pull request comment."); return None
        if 'number' in event_payload: logger.warning(f"Found 'number' ({event_payload['number']}) directly in payload, assuming it's PR number."); return int(event_payload['number'])
        logger.error("Could not reliably determine pull request number from event payload."); logger.debug(f"Event Payload Keys: {event_payload.keys()}"); return None
    except Exception as e: logger.error(f"Error reading event payload: {e}", exc_info=True); return None

def get_pull_request(pr_number: int) -> Optional[PullRequest]: # Use imported class
    """Gets the PyGithub PullRequest object."""
    try:
        pr = repo.get_pull(pr_number)
        logger.info(f"Successfully retrieved PR object for #{pr_number}")
        return pr
    except UnknownObjectException: logger.error(f"Pull Request #{pr_number} not found in {OWNER}/{REPO}."); return None
    except GithubException as e: logger.error(f"Error getting PR #{pr_number}: {e.status} {e.data}"); return None
    except Exception as e: logger.error(f"Unexpected error getting PR #{pr_number}: {e}", exc_info=True); return None

def get_issue(issue_number: int) -> Optional[Issue]: # Use imported class
    """Gets the PyGithub Issue object."""
    try:
        issue = repo.get_issue(issue_number)
        logger.info(f"Successfully retrieved Issue object for #{issue_number}")
        return issue
    except UnknownObjectException: logger.error(f"Issue #{issue_number} not found in {OWNER}/{REPO}."); return None
    except GithubException as e: logger.error(f"Error getting Issue #{issue_number}: {e.status} {e.data}"); return None
    except Exception as e: logger.error(f"Unexpected error getting Issue #{issue_number}: {e}", exc_info=True); return None

# --- Core API Functions (Original PyGithub / Requests Based) ---

def get_pr_diff(pr: PullRequest) -> Optional[str]: # Use imported class
    """Fetches the diff for a given PyGithub PullRequest object using requests."""
    if not pr: logger.error("Valid PullRequest object is required to fetch diff."); return None
    try:
        diff_url = pr.url
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3.diff", "X-GitHub-Api-Version": "2022-11-28"}
        response = requests.get(diff_url, headers=headers)
        response.raise_for_status()
        logger.info(f"Successfully fetched diff for PR #{pr.number}")
        return response.text
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching PR diff for PR #{pr.number} via requests: {e}")
        if hasattr(e, 'response') and e.response is not None: logger.error(f"Response status: {e.response.status_code}\nResponse text: {e.response.text}")
        return None
    except Exception as e: logger.error(f"An unexpected error occurred fetching diff for PR #{pr.number}: {e}", exc_info=True); return None

def find_linked_issue_number(pr: PullRequest) -> Optional[int]: # Use imported class
    """Finds the *first* issue explicitly linked to the PR using timeline events or body regex."""
    if not pr: logger.error("Valid PullRequest object is required to find linked issue."); return None
    try: # Try Timeline Events
        logger.debug(f"Attempting to fetch timeline events for PR #{pr.number} using get_issue_events()...")
        timeline = pr.get_issue_events()
        logger.debug(f"Successfully fetched timeline events object for PR #{pr.number}.")
        for event in timeline:
            if event.event == 'cross-referenced' and event.source and event.source.issue:
                 source_issue = event.source.issue
                 if source_issue.repository.full_name == f"{OWNER}/{REPO}" and source_issue.number != pr.number:
                     logger.info(f"Found linked issue #{source_issue.number} via '{event.event}' event for PR #{pr.number}.")
                     return source_issue.number
        logger.warning(f"Found no explicitly linked issue event for PR #{pr.number}. Falling back to regex check.")
    except GithubException as e_outer: logger.error(f"GitHub API error fetching timeline events for PR #{pr.number}: {e_outer.status} {e_outer.data}. Falling back to regex check.")
    except Exception as e_outer: logger.error(f"Unexpected error fetching timeline events for PR #{pr.number}: {e_outer}. Falling back to regex check.", exc_info=True)
    try: # Fallback: Regex check on PR body
        pr_body = pr.body
        if not pr_body: logger.warning(f"PR #{pr.number} body is empty. Cannot find linked issue via body text."); return None
        patterns = [ r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)[\s:]*#(\d+)" ]
        for pattern in patterns:
            match = re.search(pattern, pr_body, re.IGNORECASE)
            if match: issue_number = int(match.group(1)); logger.info(f"Found potential linked issue #{issue_number} via regex fallback in PR #{pr.number} body."); return issue_number
        logger.warning(f"Could not find linked issue number via regex fallback in PR #{pr.number} body.")
        return None
    except Exception as e_fallback: logger.error(f"An unexpected error occurred during regex fallback for PR #{pr.number}: {e_fallback}", exc_info=True); return None

def get_issue_body(issue: Issue) -> Optional[str]: # Use imported class
    """Gets the body content of a given PyGithub Issue object."""
    if not issue: logger.error("Valid Issue object is required to fetch body."); return None
    try: body = issue.body if issue.body is not None else ""; logger.info(f"Successfully retrieved body for issue #{issue.number}"); return body
    except Exception as e: logger.error(f"An unexpected error occurred getting body for issue #{issue.number}: {e}", exc_info=True); return None

def post_pr_comment(pr_or_issue_number: int, comment_body: str) -> bool:
    """Posts a comment to the specified pull request or issue number using PyGithub."""
    if not pr_or_issue_number: logger.error("PR or Issue number is required to post comment."); return False
    if not comment_body: logger.warning("Comment body is empty, not posting."); return False
    try:
        target_issue = repo.get_issue(pr_or_issue_number)
        target_issue.create_comment(comment_body)
        logger.info(f"Successfully posted comment to Issue/PR #{pr_or_issue_number}")
        return True
    except UnknownObjectException: logger.error(f"Issue/PR #{pr_or_issue_number} not found for posting comment."); return False
    except GithubException as e: logger.error(f"Error posting comment to Issue/PR #{pr_or_issue_number}: {e.status} {e.data}"); return False
    except Exception as e: logger.error(f"An unexpected error occurred posting comment to #{pr_or_issue_number}: {e}", exc_info=True); return False

def get_contextual_code(pr: PullRequest, diff_text: str) -> str: # Use imported class
    """
    Fetches relevant code context for changes identified in a diff.
    Implements the refined strategy: full definition of changed blocks + signatures of local calls.
    """
    context_parts = []
    changed_py_files = parse_diff_hunks(diff_text)
    pr_head_sha = pr.head.sha
    logger.info(f"Found {len(changed_py_files)} Python files with changes in diff.")
    parsed_file_cache: Dict[str, AstParser] = {}

    def get_parsed_file(filename: str) -> Optional[AstParser]:
        """Fetches file content using PyGithub and parses it, using a cache."""
        if filename in parsed_file_cache: return parsed_file_cache[filename]
        try:
            logger.debug(f"Fetching content for {filename} at ref {pr_head_sha} using PyGithub")
            content_item = repo.get_contents(filename, ref=pr_head_sha)
            if isinstance(content_item, list): logger.warning(f"Path {filename} is a directory, skipping."); return None
            if not isinstance(content_item, ContentFile): logger.warning(f"Expected ContentFile, but got {type(content_item)} for {filename}, skipping."); return None # Use imported class
            if content_item.type != 'file': logger.warning(f"Path {filename} is not a file (type: {content_item.type}), skipping."); return None
            file_content = content_item.decoded_content.decode("utf-8")
            logger.debug(f"Successfully fetched content for {filename}")
            parser = AstParser(file_content)
            parser.visit(ast.parse(file_content))
            logger.debug(f"Parsed {filename}. Found {len(parser.functions)} funcs, {len(parser.classes)} classes.")
            parsed_file_cache[filename] = parser
            return parser
        except UnknownObjectException: logger.error(f"File not found via PyGithub: {filename} at ref {pr_head_sha}."); return None
        except GithubException as e:
            if e.status == 403 and 'rate limit exceeded' in str(e.data).lower(): logger.error(f"Rate limit exceeded fetching content for {filename}. Skipping file.")
            else: logger.error(f"GitHub API error fetching content for {filename}: {e.status} {e.data}")
            return None
        except SyntaxError as e: logger.error(f"Syntax error parsing {filename}: {e}"); return None
        except Exception as e: logger.error(f"Unexpected error processing file {filename}: {e}", exc_info=True); return None

    for filename, changed_lines in changed_py_files.items():
        if not filename.endswith(".py"): logger.debug(f"Skipping non-Python file: {filename}"); continue
        parser = get_parsed_file(filename)
        if not parser: context_parts.append(f"\n--- Skipped File: {filename} (Could not fetch or parse) ---"); continue
        changed_blocks_in_file: List[Tuple[str, FunctionInfo, Optional[str]]] = []
        for func_name, func_info in parser.functions.items():
            if changed_lines.intersection(range(func_info.start_line, func_info.end_line + 1)): changed_blocks_in_file.append(("Function", func_info, None))
        for class_name, class_info in parser.classes.items():
             for method_name, method_info in class_info.methods.items():
                 if changed_lines.intersection(range(method_info.start_line, method_info.end_line + 1)): changed_blocks_in_file.append(("Method", method_info, class_name))
        if not changed_blocks_in_file: logger.debug(f"No function/method definitions found containing changed lines in {filename}"); continue
        file_context_parts = [f"\n--- Context from File: {filename} ---"]
        processed_signatures_for_file = set()
        processed_imports_for_file = set()
        for block_type, block_info, parent_class_name in changed_blocks_in_file:
            block_name = block_info.name
            block_display_name = f"{parent_class_name}.{block_name}" if parent_class_name else block_name
            file_context_parts.append(f"\n--- Context for Changed {block_type} `{block_display_name}` ---")
            file_context_parts.append("\nFull Definition:")
            file_context_parts.append(block_info.body_source)
            call_signatures = []
            relevant_imports = set()
            for call in block_info.calls:
                found_local = False; local_sig = None
                if call in parser.functions:
                    if call != block_name: local_sig = parser.functions[call].signature; found_local = True
                else:
                    potential_method_name = call.split('.')[-1]
                    for c_info in parser.classes.values():
                        if potential_method_name in c_info.methods:
                            if not (parent_class_name == c_info.name and potential_method_name == block_name): local_sig = c_info.methods[potential_method_name].signature; found_local = True; break
                if found_local and local_sig and call not in processed_signatures_for_file:
                    call_signatures.append(f"\nSignature of Local Function/Method Called `{call}`:"); call_signatures.append(local_sig); processed_signatures_for_file.add(call)
                elif not found_local:
                    base_call = call.split('.')[0]
                    for imp in parser.imports:
                        if (f"import {base_call}" in imp or f"from {base_call} import" in imp or f"as {base_call}" in imp) and imp not in processed_imports_for_file:
                            relevant_imports.add(imp); processed_imports_for_file.add(imp); break 
            if call_signatures: file_context_parts.extend(call_signatures)
            if relevant_imports: file_context_parts.append("\nRelevant Imports:"); file_context_parts.extend(sorted(list(relevant_imports)))
        context_parts.extend(file_context_parts)
    logger.info(f"Generated context for {len(context_parts)} parts.")
    return "\n".join(context_parts).strip()
