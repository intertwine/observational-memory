---
description: Search observational memory for past context
argument-hint: "<query>"
---

# /recall

Search observational memory for context from past sessions.

## Usage

```
/recall $ARGUMENTS
```

Run the following command to recall memory:

```bash
om recall --query "$ARGUMENTS" --limit 10
```

Present the results to the user, highlighting the most relevant matches.
If no results are found, suggest broadening the search terms.
