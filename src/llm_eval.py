import os
import logging
import re # Import the regular expression module
# Import AzureOpenAI specifically
from openai import AzureOpenAI, OpenAIError
# Removed prompty import

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Get Azure OpenAI details from environment variables set by the action inputs
AZURE_OPENAI_ENDPOINT = os.getenv("INPUT_AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_KEY = os.getenv("INPUT_AZURE_OPENAI_KEY")
AZURE_OPENAI_DEPLOYMENT = os.getenv("INPUT_AZURE_OPENAI_DEPLOYMENT")
# A common API version; check Azure portal if a different one is needed for your endpoint
AZURE_OPENAI_API_VERSION = "2024-02-01"

# Use absolute path within the container
DEFAULT_PROMPT_PATH = "/app/prompts/intent_check.prompty"

# Validate necessary Azure credentials
if not all([AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY, AZURE_OPENAI_DEPLOYMENT]):
    logger.error("Azure OpenAI credentials (Endpoint, Key, Deployment) not fully configured in environment variables.")
    # Exit or handle appropriately in main.py

def load_prompt_template_string(prompt_path=DEFAULT_PROMPT_PATH):
    """Loads the prompt template string from the specified file."""
    try:
        with open(prompt_path, 'r') as f:
            content = f.read()
        # Find the end of the YAML front matter (second '---')
        parts = content.split('---', 2)
        if len(parts) < 3:
             logger.warning(f"Could not find YAML front matter separator '---' in {prompt_path}. Using entire file content.")
             template_string = content
        else:
             template_string = parts[2].strip() # Get content after the second '---'

        if not template_string:
             logger.error(f"Prompt template string is empty after parsing {prompt_path}")
             return None

        logger.info(f"Successfully loaded prompt template string from {prompt_path}")
        return template_string
    except FileNotFoundError:
        logger.error(f"Prompt file not found at {prompt_path}")
        return None
    except Exception as e:
        logger.error(f"Error loading prompt file {prompt_path}: {e}")
        return None

def evaluate_intent(issue_body, code_diff, template_string):
    """
    Evaluates code diff against issue body using the template string and Azure OpenAI API.

    Args:
        issue_body (str): The content of the linked GitHub issue.
        code_diff (str): The diff content of the pull request.
        prompt_template (Prompt): The loaded prompty object.
        model (str): The OpenAI model to use.

    Returns:
        tuple: (result, explanation) where result is 'PASS' or 'FAIL',
               and explanation is the reasoning from the LLM.
               Returns (None, None) on failure.
    """
    if not all([AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY, AZURE_OPENAI_DEPLOYMENT]):
         logger.error("Cannot evaluate intent: Azure OpenAI credentials missing.")
         return None, "Azure OpenAI credentials (Endpoint, Key, Deployment) not configured."
    if not template_string:
        logger.error("Cannot evaluate intent: Prompt template string not loaded.")
        return None, "Prompt template string failed to load."
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
        # Initialize AzureOpenAI client
        client = AzureOpenAI(
            api_key=AZURE_OPENAI_KEY,
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            api_version=AZURE_OPENAI_API_VERSION,
        )

        # Prepare inputs for the prompt template
        # The keys ('requirements', 'code_changes') must match the {{variables}} in the .prompty file
        prompt_inputs = {
            "requirements": issue_body,
            "code_changes": code_diff
        }

        # Render the prompt using prompty (this fills in the variables)
        # Note: Prompty's rendering might evolve. Check its documentation if issues arise.
        # Assuming prompt_template acts like a callable or has a render method.
        # If prompty.load returns a string template directly, manual formatting is needed.
        # Let's assume prompty handles the rendering internally when called/executed.
        # This part might need adjustment based on how prompty library actually works.
        # For now, let's assume we pass the dict to the template execution.

        # --- Placeholder for actual prompty rendering ---
        # This is conceptual. The actual API might differ.
        # rendered_prompt_content = prompt_template.render(**prompt_inputs) # Example if it has a render method
        # Or maybe prompty handles execution directly:
        # response = prompt_template(**prompt_inputs) # If it's directly callable

        # Let's assume prompty provides a way to get the final message structure
        # for the OpenAI API call after filling variables.
        # If not, we construct it manually after filling the template string.

        # Manual construction if prompty.load just returns a string template:
        # filled_template_string = prompt_template.format(**prompt_inputs) # Basic string formatting
        # messages = [{"role": "user", "content": filled_template_string}]

        # Using prompty's execution model (preferred if available)
        # This assumes `prompt_template` object knows how to execute itself
        # with the given inputs and returns an OpenAI-compatible response object
        # or directly the text content. Adjust based on prompty's actual API.

        logger.info(f"Sending request to Azure OpenAI deployment: {AZURE_OPENAI_DEPLOYMENT}")
        # Use f-string formatting (requires {{ }} to be escaped as {{}} if literal braces are needed)
        # Or use .format() if f-strings are tricky with the template syntax
        try:
            # Using .format() as it handles named placeholders directly
            final_prompt_text = template_string.format(requirements=issue_body, code_changes=code_diff)
        except KeyError as fmt_err:
             logger.error(f"Failed to format prompt template. Missing key: {fmt_err}. Check template variables.")
             return None, f"Failed to format prompt template. Missing key: {fmt_err}."
        except Exception as fmt_err:
             logger.error(f"Failed to format prompt template: {fmt_err}")
             return None, f"Failed to format prompt template: {fmt_err}"


        chat_completion = client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": final_prompt_text,
                }
            ],
            # For Azure, the 'model' parameter is the deployment name
            model=AZURE_OPENAI_DEPLOYMENT,
            # Add other parameters like temperature, max_tokens if needed
        )

        response_content = chat_completion.choices[0].message.content
        logger.info("Received response from OpenAI.")
        logger.debug(f"LLM Raw Response:\n{response_content}")

        # Parse the response to find "Result: PASS" or "Result: FAIL"
        result_match = re.search(r"Result:\s*(PASS|FAIL)", response_content, re.IGNORECASE)

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

# Example usage (for testing locally)
# if __name__ == "__main__":
#     # Mock data
#     mock_issue_body = "Implement a function `add(a, b)` that returns the sum of two numbers."
#     mock_code_diff_pass = """
# diff --git a/calculator.py b/calculator.py
# index e69de29..1f8d2e4 100644
# --- a/calculator.py
# +++ b/calculator.py
# @@ -0,0 +1,2 @@
# +def add(a, b):
# +  return a + b
# """
#     mock_code_diff_fail = """
# diff --git a/calculator.py b/calculator.py
# index e69de29..9c4f1a8 100644
# --- a/calculator.py
# +++ b/calculator.py
# @@ -0,0 +1,2 @@
# +def subtract(a, b): # Incorrect function
# +  return a - b
# """
#     # Create a dummy prompt file for local testing: prompts/intent_check.prompty
#     # ---
#     # name: Intent Check Prompt
#     # description: Checks if code changes meet requirements.
#     # inputs:
#     #   requirements: string
#     #   code_changes: string
#     # execution:
#     #   type: llm/completion
#     #   prompt: |
#     #     Given the following requirements:
#     #     {{requirements}}
#     #
#     #     And the following code changes (diff):
#     #     {{code_changes}}
#     #
#     #     Does the code implementation successfully address and satisfy all the requirements?
#     #     Provide a brief explanation and conclude with 'Result: PASS' or 'Result: FAIL'.
#     # ---
#     # Ensure you have a dummy prompts/intent_check.prompty file
#     # and set INPUT_OPENAI_API_KEY environment variable
#
#     if not os.path.exists("prompts"): os.makedirs("prompts")
#     if not os.path.exists(DEFAULT_PROMPT_PATH):
#         with open(DEFAULT_PROMPT_PATH, "w") as f:
#             f.write("""
# ---
# name: Intent Check Prompt
# description: Checks if code changes meet requirements.
# inputs:
#   requirements: string
#   code_changes: string
# execution:
#   type: llm/completion # This execution type might need adjustment based on prompty spec
#   prompt: |
#     Given the following requirements from the issue:
#     ```
#     {{requirements}}
#     ```
#
#     And the following code changes (diff):
#     ```diff
#     {{code_changes}}
#     ```
#
#     Does the code implementation successfully address and satisfy all the requirements and acceptance criteria?
#     Provide a brief explanation and conclude with 'Result: PASS' or 'Result: FAIL'.
# ---
# """)
#
#     template = load_prompt_template()
#     if template and os.getenv("INPUT_OPENAI_API_KEY"):
#         print("--- Testing PASS case ---")
#         result_pass, exp_pass = evaluate_intent(mock_issue_body, mock_code_diff_pass, template)
#         print(f"Result: {result_pass}\nExplanation: {exp_pass}\n")
#
#         print("--- Testing FAIL case ---")
#         result_fail, exp_fail = evaluate_intent(mock_issue_body, mock_code_diff_fail, template)
#         print(f"Result: {result_fail}\nExplanation: {exp_fail}\n")
#     elif not os.getenv("INPUT_OPENAI_API_KEY"):
#         print("Skipping local test: INPUT_OPENAI_API_KEY environment variable not set.")
#     else:
#         print("Skipping local test: Could not load prompt template.")
