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
            # Ensure the method list is initialized for the class
            if self._current_class_name not in self.method_calls:
                 self.method_calls[self._current_class_name] = {}
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
                            # Ensure list exists before appending
                            if func_name in self.method_calls.get(self._current_class_name, {}):
                                self.method_calls[self._current_class_name][func_name].append(call_name)
                            else:
                                # This case should ideally not happen due to initialization above, but log if it does
                                logger.warning(f"Attempted to log call '{call_name}' for uninitialized method '{func_name}' in class '{self._current_class_name}'.")
                        else:
                             # Ensure list exists before appending
                            if func_name in self.function_calls:
                                self.function_calls[func_name].append(call_name)
                            else:
                                # This case should ideally not happen due to initialization above, but log if it does
                                logger.warning(f"Attempted to log call '{call_name}' for uninitialized function '{func_name}'.")

        self._current_function_name = None # Reset when leaving function scope

    def _get_call_name(self, node: ast.Call) -> Optional[str]:
        """Helper to get the name of the function/method being called using ast.unparse."""
        # Use ast.unparse (requires Python 3.9+)
        try:
            return ast.unparse(node.func)
        except Exception as e:
            logger.warning(f"Could not determine call name via ast.unparse for node: {ast.dump(node)} - Error: {e}", exc_info=True)
            return None # Avoid complex fallbacks if unparse fails


def parse_diff(diff: str) -> Dict[str, Set[int]]:
    """
    Parses a git diff string to find changed Python files and added line numbers.
    """
    if not diff:
        return {}

    changed_lines: Dict[str, Set[int]] = {}
    current_file: Optional[str] = None
    new_file_line_num = 0
    file_path_regex = re.compile(r"^\+\+\+ b/(.*)")
    hunk_header_regex = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    for line in diff.splitlines():
        file_match = file_path_regex.match(line)
        if file_match:
            current_file = file_match.group(1)
            if not current_file.endswith(".py"): # Focus only on Python files
                current_file = None
                continue
            if current_file not in changed_lines:
                changed_lines[current_file] = set()
            continue

        if current_file is None: continue # Skip lines if not in a python file diff

        hunk_match = hunk_header_regex.match(line)
        if hunk_match:
            new_file_line_num = int(hunk_match.group(1))
            continue

        if line.startswith('+'):
            changed_lines[current_file].add(new_file_line_num)
            new_file_line_num += 1
        elif not line.startswith('-') and not line.startswith('\\'): # Count context lines
            new_file_line_num += 1

    logger.debug(f"Parsed diff. Changed Python files and added line numbers: {changed_lines}")
    return changed_lines


def _get_node_line_range(node: ast.AST) -> Tuple[int, int]:
    """Safely get the start and end line number for an AST node."""
    start_line = getattr(node, 'lineno', -1)
    end_line = getattr(node, 'end_lineno', start_line)
    return start_line, end_line

def _analyze_python_file(file_path: str, pr: PullRequest.PullRequest) -> Optional[CodeAnalyzer]:
    """Fetches, parses, and analyzes a single Python file using CodeAnalyzer."""
    logger.info(f"Analyzing file: {file_path}")
    full_content = get_file_content(pr, file_path)

    if full_content is None:
        logger.warning(f"Could not fetch content for {file_path}. Skipping analysis.")
        return None
    if not full_content.strip():
        logger.info(f"File {file_path} is empty. Skipping analysis.")
        return None

    try:
        tree = ast.parse(full_content)
        analyzer = CodeAnalyzer()
        analyzer.visit(tree)
        return analyzer
    except SyntaxError as e:
        logger.error(f"Syntax error parsing {file_path}: {e}. Skipping analysis.")
        return None
    except Exception as e:
        logger.error(f"Unexpected error analyzing AST for {file_path}: {e}", exc_info=True)
        return None

def _find_relevant_nodes(analyzer: CodeAnalyzer, added_lines: Set[int]) -> List[Tuple[str, ast.AST]]:
    """Identifies functions/classes in the analyzer results that contain added lines."""
    relevant_nodes: List[Tuple[str, ast.AST]] = []
    processed_nodes: Set[str] = set()

    # Check functions
    for func_name, node in analyzer.function_defs.items():
        start, end = _get_node_line_range(node)
        if any(start <= line <= end for line in added_lines):
            if func_name not in processed_nodes:
                 relevant_nodes.append((func_name, node))
                 processed_nodes.add(func_name)

    # Check classes and their methods
    for class_name, node in analyzer.class_defs.items():
        if class_name in processed_nodes: continue
        start, end = _get_node_line_range(node)
        class_or_method_changed = any(start <= line <= end for line in added_lines)

        if not class_or_method_changed:
            for method_node in node.body:
                if isinstance(method_node, ast.FunctionDef):
                    m_start, m_end = _get_node_line_range(method_node)
                    if any(m_start <= line <= m_end for line in added_lines):
                        class_or_method_changed = True
                        break
        if class_or_method_changed:
            relevant_nodes.append((class_name, node))
            processed_nodes.add(class_name)

    logger.debug(f"Found {len(relevant_nodes)} relevant nodes containing changes.")
    return relevant_nodes

def _extract_node_context(node_name: str, node: ast.AST, analyzer: CodeAnalyzer, file_path: str) -> List[str]:
    """Extracts formatted context strings for a single relevant node."""
    node_context_parts: List[str] = []
    node_type = "Function" if isinstance(node, ast.FunctionDef) else "Class"

    # 1. Add Full Definition using ast.unparse
    node_context_parts.append(f"--- Full Definition of Changed {node_type} `{node_name}` (in {file_path}) ---")
    try:
        source_code = ast.unparse(node)
        node_context_parts.append(source_code)
    except Exception as e:
        logger.error(f"Could not extract source for {node_type} '{node_name}' using ast.unparse: {e}")
        node_context_parts.append(f"# Error: Could not extract source code for {node_name}")

    # 2. Add Calls Made By This Node
    calls_made: List[str] = []
    if isinstance(node, ast.FunctionDef):
        calls_made = analyzer.function_calls.get(node_name, [])
    elif isinstance(node, ast.ClassDef):
         for method_name, calls in analyzer.method_calls.get(node_name, {}).items():
             calls_made.extend(calls)

    if calls_made:
         unique_calls = sorted(list(set(calls_made)))
         node_context_parts.append(f"\n--- Calls made by `{node_name}` (or its methods) ---")
         for call in unique_calls:
             call_context = f"{call}(...)" # Default
             found_context = False
             base_name = call.split('.')[0]
             # Check imports
             for imp in analyzer.imports:
                 if f"import {base_name}" in imp or f"from {base_name}" in imp or f".{base_name}" in imp:
                     call_context += f" # Requires: {imp}"
                     found_context = True
                     break
             # Check local functions/methods if not found in imports
             if not found_context:
                 if call in analyzer.function_defs:
                     try: sig = ast.unparse(analyzer.function_defs[call].args); call_context = f"def {call}{sig}: ..."
                     except Exception: logger.warning(f"Could not unparse args for local function call '{call}'")
                 elif any(call in methods for methods in analyzer.method_calls.values()):
                     for c_name, methods in analyzer.method_calls.items():
                         if call in methods:
                             class_node = analyzer.class_defs.get(c_name)
                             method_node = next((m for m in class_node.body if isinstance(m, ast.FunctionDef) and m.name == call), None) if class_node else None
                             if method_node:
                                 try: sig = ast.unparse(method_node.args); call_context = f"def {call}{sig}: ... # Method in class {c_name}"
                                 except Exception: logger.warning(f"Could not unparse args for local method call '{call}' in class '{c_name}'")
                             break
             node_context_parts.append(call_context)

    return node_context_parts

def generate_context_code(diff: str, pr: PullRequest.PullRequest) -> str:
    """Generates the CONTEXT CODE section by analyzing changed files using AST."""
    overall_context_parts: List[str] = []
    changed_py_files = parse_diff(diff)

    if not changed_py_files:
        logger.info("No changed Python files found in the diff. No AST context generated.")
        return ""

    for file_path, added_lines in changed_py_files.items():
        analyzer = _analyze_python_file(file_path, pr)
        if not analyzer:
            overall_context_parts.append(f"--- Could not analyze {file_path} ---")
            overall_context_parts.append("\n")
            continue

        relevant_nodes = _find_relevant_nodes(analyzer, added_lines)
        if not relevant_nodes:
            logger.info(f"No specific function/class definitions found containing changes in {file_path}.")
            continue

        file_context_parts: List[str] = []
        processed_node_names: Set[str] = set()

        for node_name, node in relevant_nodes:
            if node_name in processed_node_names: continue
            processed_node_names.add(node_name)
            file_context_parts.extend(_extract_node_context(node_name, node, analyzer, file_path))

        # Add relevant imports for the file if context was generated
        if file_context_parts and analyzer.imports:
             file_context_parts.append(f"\n--- Relevant Imports from {file_path} ---")
             file_context_parts.extend(sorted(list(set(analyzer.imports))))

        if file_context_parts:
            overall_context_parts.extend(file_context_parts)
            overall_context_parts.append("\n")

    return "\n".join(overall_context_parts).strip()

# Example Usage (for testing purposes)
if __name__ == '__main__':
    print("AST Analyzer module loaded.")
    # Add test code here if needed
