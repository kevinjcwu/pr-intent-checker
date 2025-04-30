# PR Intent Checker GitHub Action

This GitHub Action uses an AI model (via Azure OpenAI Service) to analyze the code changes in a Pull Request (PR) and compare them against the requirements specified in a linked GitHub Issue. It helps identify potential "intent drift" early in the development cycle.

## How it Works

1.  **Trigger:** The action runs automatically when a Pull Request is opened or updated in a repository where it's configured.
2.  **Link Issue:** The action searches the Pull Request description for specific keywords and an issue number to identify the linked issue containing the requirements.
3.  **Fetch Data:** It fetches the PR code diff and the body of the linked issue using the GitHub API.
4.  **AI Analysis:** It sends the issue requirements and the code diff to your configured Azure OpenAI model (e.g., GPT-4o) using a predefined prompt.
5.  **Evaluation:** The AI model evaluates whether the code changes satisfy the requirements.
6.  **Report Result:** The action parses the AI's response to determine a `PASS` or `FAIL` result.
7.  **Status Check & Comment:** It sets the status check on the PR accordingly (failing the check on `FAIL`). It then posts the AI's explanation as a comment on the PR. If the result is `FAIL`, the explanation will attempt to include specific code snippets from the diff that illustrate the detected misalignment with the requirements.

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

    *(Case is ignored, e.g., `closes #123` works too).*

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
*   `explanation`: The explanation provided by the AI model. If the result is `FAIL`, this explanation may include specific code snippets (formatted using Markdown diff syntax) highlighting the areas of concern identified by the AI.

## Contributing

Feel free to submit issues or pull requests to the `kevinjcwu/pr-intent-checker` repository.
