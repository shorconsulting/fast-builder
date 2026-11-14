## Autopilot policy

This repo's automation settings are in `.github/autopilot.json` (chosen on the Build Autopilot page; never edit
it by hand). Before asking the user a question or waiting for an approval, read it and follow it: `always_ask`
lists the only decisions that must go to a person; `level` says whether to stop at the PR, merge after an AI
review, or merge and release; `questions` says whether to ask now or decide and keep going. When you decide
instead of asking, list every such decision under "Decided without you" in the pull request and in your
end-of-run summary (what you chose, why, how to undo it). Never decide silently.
