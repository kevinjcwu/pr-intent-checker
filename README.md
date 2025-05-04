# PR Intent Checker GitHub Action

This GitHub Action uses an AI model (via Azure OpenAI Service) to analyze the code changes in a Pull Request (PR) and compare them against the requirements specified in a linked GitHub Issue. It helps identify potential "intent drift" early in the development cycle.

## How it Works: Evolution of Context

The core goal of this action is to provide an AI model with enough information to determine if a PR's code changes fulfill the intent described in a linked issue. The strategy for providing this information has evolved:

**1. Initial Approach: Diff-Only Context**

*   The action was initially designed to fetch only the **PR code diff** and the **linked issue body**.
*   These two pieces of text were sent to the AI model.
*   **Limitation:** While simple, relying solely on the diff proved insufficient. The diff only shows *what lines changed*, but lacks the surrounding code structure. The AI couldn't easily see the full context of a modified function or class, potentially leading to inaccurate assessments of whether the change truly met the issue's requirements within the broader codebase structure.

**2. The Context Challenge: Diff vs. Full Files vs. AST**

*   Simply sending the **entire content of every changed file** along with the diff would provide maximum context, but this is often impractical and costly due to the large number of tokens involved, especially for large files or PRs touching many files.
*   We needed a way to provide *more* context than just the diff, but *less* than entire files, focusing on the most relevant structural information around the changes.

**3. Current Approach: AST-Based Structural Context**

*   The action now uses a more sophisticated approach leveraging **Abstract Syntax Trees (AST)** for Python files:
    *   **Trigger:** Runs when a PR is opened or updated.
    *   **Link Issue:** Identifies the linked issue via timeline events or PR body keywords.
    *   **Fetch Data:** Fetches the PR code diff, linked issue body, and the **full content** of any changed Python (`.py`) files.
    *   **Analyze Structure (AST):**
        *   Parses the full content of each changed Python file into an AST.
        *   Identifies which functions or classes within the AST contain the lines modified in the diff.
        *   Extracts the **complete definition** (source code) of these specific changed functions/classes.
        *   Gathers related context like relevant import statements and calls made *by* the changed code.
    *   **AI Analysis:** Sends the issue requirements, the code diff, and this extracted structural context (`CONTEXT CODE`) to the AI model.
    *   **Evaluation:** The AI uses the diff for line changes and the `CONTEXT CODE` to understand the structural impact and implementation details within the modified functions/classes.
    *   **Report Result:** Parses the AI response for `PASS`/`FAIL` and posts a comment on the PR, potentially including relevant code snippets from the diff or context code if the result is `FAIL`.

This AST-based approach aims to strike a balance, providing crucial structural context around the specific code being modified without incurring the excessive token cost of sending entire unchanged files or unrelated code sections. *(Note: Currently, the full definition of changed functions/classes is extracted. Future refinements might explore strategies like context windowing or signature-only extraction for very large functions to further optimize token usage if needed).*

## Understanding Abstract Syntax Trees (AST)

The current approach relies heavily on Abstract Syntax Trees (ASTs) to understand the structure of Python code. Here's a brief explanation:

**What is an AST?**

An AST is a tree-like data structure that represents the syntactic structure of source code. Think of it as a detailed outline or map of your code, ignoring superficial details like comments or whitespace, but capturing the essential components like:

*   Function definitions (`def my_func(...):`)
*   Class definitions (`class MyClass:`)
*   Assignments (`x = 5`)
*   Function calls (`print("hello")`, `other_func(x)`)
*   Control flow (loops like `for`, `while`; conditionals like `if`, `else`)
*   Imports (`import os`)

Each element becomes a "node" in the tree, connected in a way that reflects the code's organization.

**Example:**

Consider this Python code:

```python
# geometry.py
import logging

def calculate_area(length, width):
  """Calculates area, logs result."""
  if length <= 0 or width <= 0:
      return None
  area = length * width
  logging.info(f"Calculated area: {area}")
  return area

def process_dimensions(l, w):
  """Processes dimensions by calculating area."""
  print("Processing dimensions...")
  calculated_value = calculate_area(l, w)
  if calculated_value:
      print(f"Area result: {calculated_value}")
  else:
      print("Invalid dimensions for area calculation.")

# Example call
process_dimensions(10, 5)

```

An AST parser (like Python's built-in `ast` module) would turn this into a structure representing, among other things:

*   An `Import` node for `import logging`.
*   A `FunctionDef` node for `calculate_area` containing:
    *   An `arguments` node for `length`, `width`.
    *   An `If` node for the validation check.
    *   An `Assign` node for `area = ...`.
    *   A `Call` node for `logging.info(...)`.
    *   A `Return` node.
*   A `FunctionDef` node for `process_dimensions` containing:
    *   An `arguments` node for `l`, `w`.
    *   A `Call` node for `print(...)`.
    *   An `Assign` node for `calculated_value = ...` which contains:
        *   A `Call` node for `calculate_area(l, w)`.
    *   An `If` node checking `calculated_value`.
*   A `Call` node for `process_dimensions(10, 5)` at the module level.

**Why is this useful for the Action?**

By analyzing this tree structure programmatically, the action can:

*   Reliably find the exact start and end lines of specific function definitions (`calculate_area`, `process_dimensions`).
*   Identify calls made *inside* a function (e.g., `process_dimensions` calls `calculate_area`; `calculate_area` calls `logging.info`).
*   Determine which function definition(s) contain the line numbers changed in a PR diff.
*   Extract the full source code for just those specific function definitions that were modified.

This allows the action to provide targeted, structural context to the AI model, going beyond simple text analysis of the diff.

## Usage

1.  **Add Workflow:** Create a workflow file in your repository (e.g., `.github/workflows/intent_check.yml`) similar to the following:

    ```yaml
    name: PR Intent Check

    on:
      pull_request:
        types: [opened, synchronize, reopened]

    permissions:
      contents: read
      pull-requests: write
      issues: read

    jobs:
      intent-check:
        runs-on: ubuntu-latest
        steps:
          - name: Checkout code
            uses: actions/checkout@v4
            with:
              fetch-depth: 0 # Required to get diff

          - name: Run PR Intent Checker
            uses: kevinjcwu/pr-intent-checker@main # Use your action repo path
            id: intent_checker
            with:
              github_token: ${{ secrets.GITHUB_TOKEN }}
              # Pass Azure credentials from secrets
              azure_openai_endpoint: ${{ secrets.AZURE_OPENAI_ENDPOINT }}
              azure_openai_key: ${{ secrets.AZURE_OPENAI_KEY }}
              azure_openai_deployment: ${{ secrets.AZURE_OPENAI_DEPLOYMENT }}

          # Optional: Explicitly fail job if checker fails
          - name: Check result from intent checker
            if: steps.intent_checker.outputs.result == 'FAIL'
            run: |
              echo "Intent Check Failed based on LLM evaluation."
              exit 1
    ```

2.  **Configure Secrets:** In your repository's Settings -> Secrets and variables -> Actions, add the following secrets:
    *   `AZURE_OPENAI_ENDPOINT`: Your Azure OpenAI resource endpoint URL.
    *   `AZURE_OPENAI_KEY`: Your Azure OpenAI API key.
    *   `AZURE_OPENAI_DEPLOYMENT`: The deployment name of your model (e.g., `gpt-4o-wukev`).
    *   *(Note: Also add these secrets to the `pr-intent-checker` action repository itself if you haven't already).*

3.  **Link Issues in PRs:** When creating a Pull Request, **you MUST include a line in the PR description** that links the relevant issue using one of the supported formats. This tells the action where to find the requirements.

    **Supported Formats:**
    Include one of the following keywords, followed by optional whitespace or a colon, then `#` and the issue number:

    *   `Closes #<number>`
    *   `Closes: #<number>`
    *   `Closed #<number>`
    *   `Fixes #<number>`
    *   `Fixes: #<number>`
    *   `Fixed #<number>`
    *   `Resolves #<number>`
    *   `Resolves: #<number>`
    *   `Resolved #<number>`

    *(Case is ignored, e.g., `closes #123` works too). The action first checks timeline events for explicit links, then falls back to checking the PR body.*

    **Example PR Description:**

    ```markdown
    This PR implements the factorial function.

    Closes #4
    ```

## Inputs

*   `github_token`: (Required) The GitHub token. Usually `${{ secrets.GITHUB_TOKEN }}`.
*   `azure_openai_endpoint`: (Required) Your Azure OpenAI endpoint URL.
*   `azure_openai_key`: (Required) Your Azure OpenAI API key.
*   `azure_openai_deployment`: (Required) Your Azure OpenAI model deployment name.

## Outputs

*   `result`: The result of the evaluation (`PASS` or `FAIL`).
*   `explanation`: The explanation provided by the AI model. If the result is `FAIL`, this explanation may include specific code snippets (formatted using Markdown diff or code syntax) highlighting the areas of concern identified by the AI, potentially referencing the context code provided.
