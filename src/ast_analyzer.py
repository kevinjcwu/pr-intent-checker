import ast
import logging
import re
from typing import Optional, Dict, List, Tuple, Set, Any

# Import necessary types and functions from github_api
from github import PullRequest
from github_api import get_file_content

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

class CodeAnalyzer(ast.NodeVisitor):
    """
    Visits AST nodes to collect information about functions, classes, calls, and imports.
    """
    def __init__(self):
        self.imports: List[str] = []
        self.function_defs: Dict[str, ast.FunctionDef] = {}
        self.class_defs: Dict[str, ast.ClassDef] = {}
        self.function_calls: Dict[str, List[str]] = {} # Calls made *within* a function {func_name: [call_names]}
        self.method_calls: Dict[str, Dict[str, List[str]]] = {} # Calls made *within* a method {class_name: {method_name: [call_names]}}
        self._current_class_name: Optional[str] = None
        self._current_function_name: Optional[str] = None # Can be function or method

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.imports.append(f"import {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        module = node.module or "" # Handle 'from . import ...'
        names = ', '.join(alias.name for alias in node.names)
        self.imports.append(f"from {module} import {names}")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        self._current_class_name = node.name
        self.class_defs[node.name] = node
        self.method_calls[node.name] = {} # Initialize dict for methods of this class
        self.generic_visit(node) # Visit methods etc. inside the class
        self._current_class_name = None # Reset when leaving class scope

    def visit_FunctionDef(self, node: ast.FunctionDef):
        func_name = node.name
        self._current_function_name = func_name

        if self._current_class_name:
            # This is a method
            self.method_calls[self._current_class_name][func_name] = []
        else:
            # This is a standalone function
            self.function_defs[func_name] = node
            self.function_calls[func_name] = []

        # Find calls within this function/method body
        for body_item in node.body:
            for sub_node in ast.walk(body_item):
                if isinstance(sub_node, ast.Call):
                    call_name = self._get_call_name(sub_node)
                    if call_name:
                        if self._current_class_name:
                            self.method_calls[self._current_class_name][func_name].append(call_name)
                        else:
                            self.function_calls[func_name].append(call_name)

        # Don't call generic_visit if we manually walked the body for calls
        # self.generic_visit(node) # If needed for args, decorators etc.
        self._current_function_name = None # Reset when leaving function scope

    def _get_call_name(self, node: ast.Call) -> Optional[str]:
        """Helper to get the name of the function/method being called."""
        try:
            # Simple name call (e.g., print(), local_func())
            if isinstance(node.func, ast.Name):
                return node.func.id
            # Attribute call (e.g., math.sqrt(), self.method(), obj.attr.method())
            elif isinstance(node.func, ast.Attribute):
                 # Attempt to reconstruct the full call chain (e.g., os.path.join)
                 # This can be complex, ast.unparse is best if available
                 try:
                     return ast.unparse(node.func)
                 except AttributeError: # Fallback for older Python or complex cases
                     # Simple obj.method
                     if isinstance(node.func.value, ast.Name):
                         return f"{node.func.value.id}.{node.func.attr}"
                     # Fallback for more complex chains like obj.attr.method
                     else:
                         return f"?.{node.func.attr}" # Indicate unknown base
            # Other complex call types (e.g., subscript calls like d['key']()) - ignore for now
            else:
                return None
        except Exception:
            # Handle potential errors during name reconstruction
            logger.warning(f"Could not determine call name for node: {ast.dump(node)}", exc_info=True)
            return None


def parse_diff(diff: str) -> Dict[str, Set[int]]:
    """
    Parses a git diff string to find changed files and the line numbers added in the new version.

    Args:
        diff: The git diff string.

    Returns:
        A dictionary where keys are file paths and values are sets of added line numbers
        (relative to the *new* file). Returns empty dict if diff is None or empty.
    """
    if not diff:
        return {}

    changed_lines: Dict[str, Set[int]] = {}
    current_file: Optional[str] = None
    new_file_line_num = 0

    # Regex to find file paths in the diff header (e.g., +++ b/path/to/file.py)
    file_path_regex = re.compile(r"^\+\+\+ b/(.*)")
    # Regex to find hunk headers (e.g., @@ -1,4 +1,5 @@)
    hunk_header_regex = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    for line in diff.splitlines():
        file_match = file_path_regex.match(line)
        if file_match:
            current_file = file_match.group(1)
            if current_file not in changed_lines:
                changed_lines[current_file] = set()
            continue # Move to next line after finding file path

        if current_file is None:
            continue # Skip lines before the first file header

        hunk_match = hunk_header_regex.match(line)
        if hunk_match:
            # Reset line number at the start of each hunk based on the '+' part
            new_file_line_num = int(hunk_match.group(1))
            continue # Move to next line after finding hunk header

        # Process lines within a hunk
        if line.startswith('+'):
            # This line was added, record its number in the new file
            changed_lines[current_file].add(new_file_line_num)
            new_file_line_num += 1
        elif line.startswith('-'):
            # This line was removed, don't increment new file line number
            pass
        elif not line.startswith('\\'): # Ignore '\ No newline at end of file'
            # This line is context, increment new file line number
            new_file_line_num += 1

    # Filter out non-python files if desired (optional)
    python_files = {f: lines for f, lines in changed_lines.items() if f.endswith(".py")}

    logger.debug(f"Parsed diff. Changed Python files and added line numbers: {python_files}")
    return python_files


def _get_node_line_range(node: ast.AST) -> Tuple[int, int]:
    """Safely get the start and end line number for an AST node."""
    start_line = getattr(node, 'lineno', -1)
    end_line = getattr(node, 'end_lineno', start_line) # Use start_line if end_lineno missing
    return start_line, end_line


def generate_context_code(diff: str, pr: PullRequest.PullRequest) -> str:
    """
    Generates the CONTEXT CODE section by analyzing changed files using AST.

    Args:
        diff: The git diff string for the PR.
        pr: The PyGithub PullRequest object.

    Returns:
        A formatted string containing the context code, or an empty string if no context found.
    """
    context_parts: List[str] = []
    changed_py_files = parse_diff(diff)

    if not changed_py_files:
        logger.info("No changed Python files found in the diff. No AST context generated.")
        return ""

    for file_path, added_lines in changed_py_files.items():
        logger.info(f"Analyzing changed file: {file_path}")
        full_content = get_file_content(pr, file_path)

        if full_content is None:
            logger.warning(f"Could not fetch content for {file_path}. Skipping AST analysis for this file.")
            continue

        if not full_content.strip():
            logger.info(f"File {file_path} is empty. Skipping AST analysis.")
            continue

        try:
            tree = ast.parse(full_content)
            analyzer = CodeAnalyzer()
            analyzer.visit(tree)

            # Find functions/classes containing the added lines
            relevant_nodes: List[Tuple[str, ast.AST]] = [] # List of (name, node)
            for func_name, node in analyzer.function_defs.items():
                start, end = _get_node_line_range(node)
                if any(start <= line <= end for line in added_lines):
                    relevant_nodes.append((func_name, node))
            for class_name, node in analyzer.class_defs.items():
                 start, end = _get_node_line_range(node)
                 # Check if class definition itself changed or any of its methods changed
                 class_or_method_changed = any(start <= line <= end for line in added_lines)
                 if not class_or_method_changed:
                     # Check methods within the class
                     for method_node in node.body:
                         if isinstance(method_node, ast.FunctionDef):
                             m_start, m_end = _get_node_line_range(method_node)
                             if any(m_start <= line <= m_end for line in added_lines):
                                 class_or_method_changed = True
                                 break # Found a changed method
                 if class_or_method_changed:
                     relevant_nodes.append((class_name, node)) # Add the whole class if it or a method changed

            if not relevant_nodes:
                logger.info(f"No specific function/class definitions found containing changes in {file_path}.")
                continue # Move to next file

            # --- Build Context for this File ---
            file_context_parts: List[str] = []
            processed_node_names: Set[str] = set() # Avoid duplicating nodes

            for node_name, node in relevant_nodes:
                if node_name in processed_node_names:
                    continue
                processed_node_names.add(node_name)

                node_type = "Function" if isinstance(node, ast.FunctionDef) else "Class"
                file_context_parts.append(f"--- Full Definition of Changed {node_type} `{node_name}` (in {file_path}) ---")
                try:
                    # Use ast.unparse if available (Python 3.9+)
                    source_code = ast.unparse(node)
                except AttributeError:
                    # Fallback: Try to slice from original content (less reliable)
                    start_line, end_line = _get_node_line_range(node)
                    if start_line > 0 and end_line >= start_line:
                         source_code = "\n".join(full_content.splitlines()[start_line-1:end_line])
                    else:
                         source_code = f"# Could not extract source for {node_name}"
                file_context_parts.append(source_code)

                # Add context about calls made *by* this function/methods of class
                calls_made: List[str] = []
                if isinstance(node, ast.FunctionDef):
                    calls_made = analyzer.function_calls.get(node_name, [])
                elif isinstance(node, ast.ClassDef):
                     for method_name, calls in analyzer.method_calls.get(node_name, {}).items():
                         calls_made.extend(calls)

                if calls_made:
                     file_context_parts.append(f"\n--- Calls made by `{node_name}` (or its methods) ---")
                     # Try to find signatures/imports for these calls
                     for call in set(calls_made): # Use set to avoid duplicates
                         if '.' not in call: # Likely local function/method call
                             if call in analyzer.function_defs:
                                 sig = ast.unparse(analyzer.function_defs[call].args) # Get args part
                                 file_context_parts.append(f"def {call}{sig}: ...")
                             elif any(call in methods for methods in analyzer.method_calls.values()):
                                 # Find which class it belongs to (simplified)
                                 for c_name, methods in analyzer.method_calls.items():
                                     if call in methods:
                                         # Find the method node within the class node
                                         class_node = analyzer.class_defs.get(c_name)
                                         method_node = next((m for m in class_node.body if isinstance(m, ast.FunctionDef) and m.name == call), None) if class_node else None
                                         if method_node:
                                             sig = ast.unparse(method_node.args)
                                             file_context_parts.append(f"def {call}{sig}: ... # Method in class {c_name}")
                                         else:
                                              file_context_parts.append(f"{call}(...) # Local method call")
                                         break
                             else:
                                 file_context_parts.append(f"{call}(...) # Local call, definition not found in file?")
                         else: # Likely imported or attribute call
                             # Find relevant import statement
                             base_name = call.split('.')[0]
                             found_import = False
                             for imp in analyzer.imports:
                                 if f"import {base_name}" in imp or f"from {base_name}" in imp or f".{base_name}" in imp:
                                     file_context_parts.append(f"{call}(...) # Requires: {imp}")
                                     found_import = True
                                     break
                             if not found_import:
                                 file_context_parts.append(f"{call}(...) # Imported or attribute call")


            # Add relevant imports for the file
            if analyzer.imports:
                 file_context_parts.append(f"\n--- Relevant Imports from {file_path} ---")
                 # Could filter imports based on calls made, but let's include all for now
                 file_context_parts.extend(analyzer.imports)

            context_parts.extend(file_context_parts)
            context_parts.append("\n") # Add separator between files

        except SyntaxError as e:
            logger.error(f"Syntax error parsing {file_path}: {e}. Skipping AST analysis for this file.")
            context_parts.append(f"--- Error Analyzing {file_path} ---")
            context_parts.append(f"Could not parse file due to SyntaxError: {e}")
            context_parts.append("\n")
        except Exception as e:
            logger.error(f"Unexpected error analyzing {file_path}: {e}", exc_info=True)
            context_parts.append(f"--- Error Analyzing {file_path} ---")
            context_parts.append(f"An unexpected error occurred: {e}")
            context_parts.append("\n")


    return "\n".join(context_parts).strip()

# Example Usage (for testing purposes)
if __name__ == '__main__':
    # This block will only run when the script is executed directly
    # You would need to mock the PR object and get_file_content for real testing
    print("AST Analyzer module loaded.")
    # Add test code here if needed, e.g.:
    # test_diff = """
    # --- a/sample.py
    # +++ b/sample.py
    # @@ -1,3 +1,4 @@
    #  def hello():
    #      print("Hello")
    # +    print("World")
    # """
    # changed = parse_diff(test_diff)
    # print(changed)
    # # Mock PR and get_file_content would be needed for generate_context_code
