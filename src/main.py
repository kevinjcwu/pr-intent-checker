import os
import sys
import logging
from github_api import (
    get_pr_details_from_event,
    get_pr_diff,
    find_linked_issue_number,
    get_issue_body,
    post_pr_comment
)
# Updated import name again
from llm_eval import evaluate_intent # Removed load_prompt_template_string

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def set_action_output(name, value):
    """Sets an output variable for the GitHub Action."""
    # Check if value is multiline and format accordingly
    if isinstance(value, str) and '\n' in value:
        # Use heredoc format for multiline outputs
        print(f'echo "{name}<<EOF" >> $GITHUB_OUTPUT')
        print(f'echo "{value}" >> $GITHUB_OUTPUT')
        print(f'echo "EOF" >> $GITHUB_OUTPUT')
    else:
        # Standard format for single-line outputs
        print(f'echo "{name}={value}" >> $GITHUB_OUTPUT')


def main():
    logger.info("Starting PR Intent Checker action...")

    # --- 1. Get PR Information ---
    pr_number = get_pr_details_from_event()
    if not pr_number:
        logger.error("Failed to determine PR number from event payload. Exiting.")
        sys.exit(1)
    logger.info(f"Processing PR #{pr_number}")

    # --- 2. Get PR Diff ---
    code_diff = get_pr_diff(pr_number)
    if code_diff is None: # Check for None explicitly, as empty diff might be valid
        logger.error(f"Failed to fetch diff for PR #{pr_number}. Exiting.")
        # Optionally set output before exiting
        set_action_output("result", "FAIL")
        set_action_output("explanation", "Error: Could not fetch PR diff.")
        sys.exit(1)
    if not code_diff:
        logger.warning(f"PR #{pr_number} has an empty diff.")
        # Decide how to handle empty diffs - maybe pass?
        # For now, let LLM decide based on prompt.

    # --- 3. Find and Get Linked Issue ---
    issue_number = find_linked_issue_number(pr_number)
    if not issue_number:
        logger.warning(f"Could not find linked issue for PR #{pr_number}.")
        # Decide how to handle: fail, pass, or skip? Let's fail for now.
        set_action_output("result", "FAIL")
        set_action_output("explanation", "Error: No linked issue found in PR body (e.g., 'Closes #123').")
        sys.exit(1) # Fail the check if no issue is linked
    logger.info(f"Found linked issue #{issue_number}")

    issue_body = get_issue_body(issue_number)
    if issue_body is None: # Check for None explicitly
        logger.error(f"Failed to fetch body for issue #{issue_number}. Exiting.")
        set_action_output("result", "FAIL")
        set_action_output("explanation", f"Error: Could not fetch body for linked issue #{issue_number}.")
        sys.exit(1)
    if not issue_body:
         logger.warning(f"Linked issue #{issue_number} has an empty body. Evaluation might be inaccurate.")
          # Proceed, but the LLM might struggle.

    # --- 4. Evaluate Intent using LLM --- (Removed template loading step)
    logger.info("Evaluating PR intent using LLM via prompty.execute...")
    # Call evaluate_intent without the template string
    result, explanation = evaluate_intent(issue_body, code_diff)

    if result is None:
        logger.error("LLM evaluation failed.")
        set_action_output("result", "FAIL")
        set_action_output("explanation", explanation or "Error: LLM evaluation failed unexpectedly.")
        sys.exit(1)

    logger.info(f"LLM Evaluation Result: {result}")

    # --- 6. Set Outputs and Exit ---
    set_action_output("result", result)
    set_action_output("explanation", explanation)

    # Optional: Post the explanation as a PR comment
    comment_header = f"🤖 **PR Intent Check Result: {result}**\n\n"
    post_pr_comment(pr_number, comment_header + explanation)

    if result == "PASS":
        logger.info("PR Intent Check Passed.")
        sys.exit(0) # Exit with success code
    else:
        logger.error("PR Intent Check Failed.")
        sys.exit(1) # Exit with failure code to fail the workflow step

if __name__ == "__main__":
    main()
