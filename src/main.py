import os
import sys
import logging
from github_api import (
    get_pr_number_from_event, # Renamed/updated function
    get_pull_request,         # New helper
    get_issue,                # New helper
    get_pr_diff,              # Takes PR object now
    find_linked_issue_number, # Takes PR object now
    get_issue_body,           # Takes Issue object now
    post_pr_comment           # Takes number, signature unchanged externally
)
from llm_eval import evaluate_intent
from ast_analyzer import generate_context_code # Import the new function

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def set_action_output(name, value):
    """Sets an output variable for the GitHub Action."""
    # Ensure value is a string before checking for newline
    str_value = str(value) if value is not None else ""
    if '\n' in str_value:
        # Use heredoc format for multiline outputs
        # Escape special characters in the value for the shell
        escaped_value = str_value.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$')
        print(f'echo "{name}<<EOF" >> $GITHUB_OUTPUT')
        print(f'{escaped_value}" >> $GITHUB_OUTPUT') # No echo needed inside heredoc
        print(f'echo "EOF" >> $GITHUB_OUTPUT')
    else:
        # Standard format for single-line outputs
        # Escape special characters
        escaped_value = str_value.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$')
        print(f'echo "{name}={escaped_value}" >> $GITHUB_OUTPUT')


def main():
    logger.info("Starting PR Intent Checker action...")

    # --- 1. Get PR Information ---
    pr_number = get_pr_number_from_event()
    if not pr_number:
        logger.error("Failed to determine PR number from event payload. Exiting.")
        set_action_output("result", "FAIL")
        set_action_output("explanation", "Error: Could not determine PR number from event.")
        sys.exit(1)
    logger.info(f"Processing PR #{pr_number}")

    # Get the PR object using the number
    pr = get_pull_request(pr_number)
    if not pr:
        # Error logged within get_pull_request
        set_action_output("result", "FAIL")
        set_action_output("explanation", f"Error: Could not retrieve PR object for #{pr_number}.")
        sys.exit(1)

    # --- 2. Get PR Diff ---
    # Pass the PR object to the function
    code_diff = get_pr_diff(pr)
    if code_diff is None: # Check for None explicitly
        logger.error(f"Failed to fetch diff for PR #{pr_number}. Exiting.")
        set_action_output("result", "FAIL")
        set_action_output("explanation", "Error: Could not fetch PR diff.")
        sys.exit(1)
    if not code_diff:
        logger.warning(f"PR #{pr_number} has an empty diff.")
        # Let LLM decide based on prompt.

    # --- 3. Find and Get Linked Issue ---
    # Pass the PR object to the function
    issue_number = find_linked_issue_number(pr)
    if not issue_number:
        logger.error(f"Could not find explicitly linked issue for PR #{pr_number}.")
        set_action_output("result", "FAIL")
        set_action_output("explanation", "Error: No explicitly linked issue found via timeline events for PR.")
        # Note: We removed the regex fallback, so this is now a hard failure.
        sys.exit(1)
    logger.info(f"Found linked issue #{issue_number}")

    # Get the Issue object using the number
    issue = get_issue(issue_number)
    if not issue:
        # Error logged within get_issue
        set_action_output("result", "FAIL")
        set_action_output("explanation", f"Error: Could not retrieve Issue object for #{issue_number}.")
        sys.exit(1)

    # Pass the Issue object to the function
    issue_body = get_issue_body(issue)
    if issue_body is None: # Check for None explicitly (though get_issue_body now returns "" for null)
        logger.error(f"Failed to fetch body for issue #{issue_number}. Exiting.")
        set_action_output("result", "FAIL")
        set_action_output("explanation", f"Error: Could not fetch body for linked issue #{issue_number}.")
        sys.exit(1)
    if not issue_body:
         logger.warning(f"Linked issue #{issue_number} has an empty body. Evaluation might be inaccurate.")
          # Proceed.

    # --- 4. Generate AST Context ---
    logger.info("Generating AST context code from diff and file contents...")
    context_code = generate_context_code(code_diff, pr)
    if not context_code:
        logger.warning("AST context code generation resulted in empty context. Proceeding without it.")
        # Optionally, you could decide to fail here if context is critical
        # context_code = "Context could not be generated." # Or provide a placeholder

    # --- 5. Evaluate Intent using LLM (with context) ---
    logger.info("Evaluating PR intent using LLM via prompty.execute...")
    # Pass the generated context_code to the evaluation function
    result, explanation = evaluate_intent(issue_body, code_diff, context_code)

    if result is None:
        logger.error("LLM evaluation failed.")
        set_action_output("result", "FAIL")
        set_action_output("explanation", explanation or "Error: LLM evaluation failed unexpectedly.")
        sys.exit(1)

    logger.info(f"LLM Evaluation Result: {result}")

    # --- 6. Set Outputs and Post Comment ---
    set_action_output("result", result)
    set_action_output("explanation", explanation)

    # Post the explanation as a PR comment
    comment_header = f"🤖 **PR Intent Check Result: {result}**\n\n"
    # Pass the original pr_number here, as post_pr_comment takes the number
    comment_posted = post_pr_comment(pr_number, comment_header + (explanation or "No explanation provided."))
    if not comment_posted:
        logger.warning(f"Failed to post comment to PR #{pr_number}.") # Don't fail the action for this

    # --- 7. Exit with appropriate status ---
    if result == "PASS":
        logger.info("PR Intent Check Passed.")
        sys.exit(0) # Exit with success code
    else:
        logger.error("PR Intent Check Failed.")
        sys.exit(1) # Exit with failure code to fail the workflow step

if __name__ == "__main__":
    main()
