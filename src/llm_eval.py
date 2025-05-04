import os
import os
import logging
import re # Import the regular expression module
import prompty
import prompty.azure # Import to register the Azure invoker
from openai import OpenAIError # Still needed for error handling
import tiktoken # For token counting

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Get Azure OpenAI details from environment variables set by the action inputs
AZURE_OPENAI_ENDPOINT = os.getenv("INPUT_AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_KEY = os.getenv("INPUT_AZURE_OPENAI_KEY")
AZURE_OPENAI_DEPLOYMENT = os.getenv("INPUT_AZURE_OPENAI_DEPLOYMENT")
# A common API version; check Azure portal if a different one is needed for your endpoint
AZURE_OPENAI_API_VERSION = "2024-02-01"

logger.debug(f"Azure OpenAI Endpoint: {AZURE_OPENAI_ENDPOINT}")
logger.debug(f"Azure OpenAI Deployment: {AZURE_OPENAI_DEPLOYMENT}")
logger.debug(f"Azure OpenAI API Version: {AZURE_OPENAI_API_VERSION}")

# Use absolute path within the container
DEFAULT_PROMPT_PATH = "/app/prompts/intent_check.prompty"

# No need to validate credentials here, prompty should handle it via env vars

# Removed load_prompt_template_string function

# --- Token Counting Helper ---
# Cache the tokenizer encoding
_tokenizer = None
_tokenizer_model = "gpt-4" # Assume gpt-4 encoding, adjust if needed

def count_tokens(text: str) -> int:
    """Counts tokens using tiktoken for the default model encoding."""
    global _tokenizer
    if not text:
        return 0
    if _tokenizer is None:
        try:
            # Use encoding for cl100k_base which is common for GPT-4, GPT-3.5-turbo, text-embedding-ada-002
            # If using a different model family, you might need a different encoding name.
            _tokenizer = tiktoken.get_encoding("cl100k_base")
            # Alternatively, load by model name, but this requires network access on first run:
            # _tokenizer = tiktoken.encoding_for_model(_tokenizer_model)
            logger.info(f"Initialized tiktoken tokenizer with encoding '{_tokenizer.name}'.")
        except Exception as e:
            logger.error(f"Failed to initialize tiktoken tokenizer: {e}. Token counts will be inaccurate.", exc_info=True)
            return -1 # Indicate error
    try:
        return len(_tokenizer.encode(text))
    except Exception as e:
        logger.error(f"Error encoding text with tiktoken: {e}", exc_info=True)
        return -1 # Indicate error


def evaluate_intent(issue_body: str, code_diff: str, contextual_code: str):
    """
    Evaluates code diff against issue body using prompty.execute and Azure OpenAI API,
    incorporating contextual code snippets.

    Args:
        issue_body (str): The content of the linked GitHub issue.
        code_diff (str): The diff content of the pull request.
        contextual_code (str): Extracted contextual code snippets (definitions, signatures, imports).

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
            "requirements": issue_body,
            "code_changes": code_diff,
            "context_code": contextual_code # Add the new context
        }

        # Log token counts for debugging
        req_tokens = count_tokens(issue_body)
        diff_tokens = count_tokens(code_diff)
        context_tokens = count_tokens(contextual_code)
        logger.debug(f"Approximate token counts for inputs: {{ requirements: {req_tokens}, code_changes: {diff_tokens}, context_code: {context_tokens} }}")

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
