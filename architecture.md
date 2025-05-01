```mermaid

graph TD
    subgraph "GitHub Cloud Environment"
        direction LR
        A_GHRepo[GitHub Repository] -- "1 - Triggers Action" --> B_Runner(GitHub Action Runner)
        B_Runner -- "2 - Executes" --> C_ActionCode[Action Code Python]
        %% Label simplified
        C_ActionCode -- "3 - Calls API" --> D_GHApi[GitHub API]
        D_GHApi -- "4 - Posts Comment" --> A_GHRepo
    end

    subgraph "User's Local Dev Env"
        direction LR
        E_User((User)) -- "6 - Interacts" --> F_DevTool[VS Code + Agent]
        F_DevTool -- "7 - Uses MCP" --> G_MCPServer(Local GitHub MCP Server)
        G_MCPServer -- "8 - Calls API" --> D_GHApi
        F_DevTool -- "9 - Modifies" --> H_LocalFiles[Local Workspace Files]
        E_User -- "10 - Pushes Fix" --> A_GHRepo
    end

    A_GHRepo -- "5 - User Observes Comment" --> E_User

    style B_Runner fill:#f9f,stroke:#333,stroke-width:2px
    style G_MCPServer fill:#ccf,stroke:#333,stroke-width:2px
    style F_DevTool fill:#ccf,stroke:#333,stroke-width:2px
    style E_User fill:#bbf,stroke:#333,stroke-width:2px
```

**Explanation:**

This diagram shows the two main environments involved:

1.  **GitHub Cloud:** Where the `pr-intent-checker` GitHub Action runs automatically (Steps 1-4). It executes the Python code, calls the GitHub API directly, and posts its results (e.g., a FAIL comment) back to the repository.
2.  **User's Local Environment:** Where the developer works (Steps 5-10). The User observes the comment posted by the Action (Step 5). If a fix is needed, the User interacts with their local tools (VS Code + Agent). The Agent uses the local GitHub MCP Server (which also calls the GitHub API) to get information (like the comment details) and modifies the local code files. Finally, the User pushes the corrected code back to GitHub.

The key takeaway is the separation: the Action runs independently in the cloud, while the MCP server assists the developer locally during the manual fixing process initiated by the User.
