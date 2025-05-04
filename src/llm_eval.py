import os
import logging
import re # Import the regular expression module
import prompty
import prompty.azure # Import to register the Azure invoker
from openai import OpenAIError # Still needed for error handling
import tiktoken # Import tiktoken for token counting

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Get Azure OpenAI details from environment variables set by the action inputs
AZURE_OPENAI_ENDPOINT = os.getenv("INPUT_AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_KEY = os.getenv("INPUT_AZURE_OPENAI_KEY")
AZURE_OPENAI_DEPLOYMENT = os.getenv("INPUT_AZURE_OPENAI_DEPLOYMENT")
# A common API version; check Azure portal if a different one is needed for your endpoint
AZURE_OPENAI_API_VERSION = "2024-02-01"

# Use absolute path within the container
DEFAULT_PROMPT_PATH = "/app/prompts/intent_check.prompty"

# No need to validate credentials here, prompty should handle it via env vars

# Removed load_prompt_template_string function

# Import Tuple type hint
from typing import Tuple, Optional

def evaluate_intent(issue_body: str, code_diff: str, context_code: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Evaluates code diff against issue body using prompty.execute and Azure OpenAI API,
    incorporating AST-based code context.

    Args:
        issue_body (str): The content of the linked GitHub issue.
        code_diff (str): The diff content of the pull request.
        context_code (str): The AST-derived context code string.

    Returns:
        tuple: (result, explanation) where result is 'PASS' or 'FAIL',
               and explanation is the reasoning from the LLM.
               Returns (None, None) on failure.
    """
    # Basic input validation
    if not issue_body:
        # Handle cases where issue body might be empty or couldn't be fetched
        logger.warning("Issue body is empty. Evaluation might be inaccurate.")
        # Decide if you want to proceed or fail early
        # return "FAIL", "Linked issue body is empty or could not be fetched."
    if not code_diff:
        logger.warning("Code diff is empty. Assuming no changes align with intent.")
        # Decide if this should be an automatic pass/fail or handled differently
        return "PASS", "No code changes detected in the PR diff." # Or FAIL? Needs consideration.

    try:
        logger.info(f"Executing prompt file: {DEFAULT_PROMPT_PATH}")
        # Prepare inputs dictionary for prompty.execute
        prompt_inputs = {
            "requirements": issue_body or "", # Ensure strings for token counting
            "code_changes": code_diff or "",
            "context_code": context_code or "" # Add the generated context
        }

        # --- Debug Logging for Inputs and Tokens ---
        logger.debug("--- Prompt Inputs ---")
        logger.debug(f"Requirements:\n{prompt_inputs['requirements']}")
        logger.debug(f"Code Changes (Diff):\n{prompt_inputs['code_changes']}")
        logger.debug(f"Context Code:\n{prompt_inputs['context_code']}")
        logger.debug("---------------------")

        try:
            # Estimate token counts using tiktoken (e.g., for gpt-4)
            # Use cl100k_base encoding as it's common for newer models
            encoding = tiktoken.get_encoding("cl100k_base")
            req_tokens = len(encoding.encode(prompt_inputs['requirements']))
            diff_tokens = len(encoding.encode(prompt_inputs['code_changes']))
            context_tokens = len(encoding.encode(prompt_inputs['context_code']))
            total_tokens = req_tokens + diff_tokens + context_tokens
            logger.debug(f"Estimated Input Token Counts (cl100k_base):")
            logger.debug(f"  Requirements: {req_tokens}")
            logger.debug(f"  Code Changes: {diff_tokens}")
            logger.debug(f"  Context Code: {context_tokens}")
            logger.debug(f"  Total Input Tokens: {total_tokens}")
        except Exception as e:
            logger.warning(f"Could not estimate token count: {e}")
        # --- End Debug Logging ---


        # Execute the prompt using the library
        # prompty should read the model config and env vars from the file
        response_content = prompty.execute(DEFAULT_PROMPT_PATH, inputs=prompt_inputs)

        if not isinstance(response_content, str):
             # Handle cases where execute might return non-string (e.g., structured object)
             # This depends on prompty's behavior, adjust as needed.
             logger.warning(f"prompty.execute returned non-string type: {type(response_content)}. Attempting conversion.")
             response_content = str(response_content)

        logger.info("Received response via prompty.execute.")
        logger.debug(f"LLM Raw Response:\n{response_content}")

        # Parse the response to find "Result: PASS" or "Result: FAIL", allowing for optional surrounding asterisks
        result_match = re.search(r"\*?\*?Result:\*?\*?\s*(PASS|FAIL)", response_content, re.IGNORECASE | re.MULTILINE)

        if result_match:
            result = result_match.group(1).upper()
            # Explanation could be the whole text or text before/after the result line
            explanation = response_content.strip()
            logger.info(f"LLM Evaluation Result: {result}")
            return result, explanation
        else:
            logger.warning("Could not parse PASS/FAIL result from LLM response.")
            # Return the full response as explanation for debugging
            return "FAIL", f"Could not parse result from LLM response:\n{response_content}"

    except OpenAIError as e:
        logger.error(f"Azure OpenAI API error: {e}")
        return None, f"Azure OpenAI API error: {e}"
    except Exception as e:
        logger.error(f"An unexpected error occurred during LLM evaluation: {e}")
        return None, f"An unexpected error occurred: {e}"
