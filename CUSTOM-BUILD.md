# Unsloth Custom

This fork adds project Git operations and GitHub repository import, removes browser/linked-folder file-count caps and per-response tool-call count caps, adds durable goals with pause/stop and limits, and permits text steering while Deep Research is running.

Git and goals are available in each project's Sources tab and Edit project dialog. The Create project dialog accepts a GitHub repository URL. Git uses the installed Git executable and existing credential manager. Remote actions are explicit; push and commit have confirmation controls. Hooks are disabled for operations from this panel. Pulls only fast-forward and no operation force-pushes.

Goals retain the selected local model or configured provider reference. Credentials stay in the existing provider store. Goals run one at a time per account, keep saved chat progress, resume when Studio launches, and stop at the configured turn or active-time limit. Output tokens per model turn are configurable. Paused/stopped goals stay stopped after restart. Model-reported completion is identified in the saved chat; users should review its evidence. A backend restart can retry an interrupted turn.

Research steering is saved durably and applied at the next research step or synthesis pass. The existing evidence and plan are retained. Text steering is available from the research card, normal composer and queued-prompt controls. It does not interrupt an HTTP request already in flight.

The Windows installer targets the existing per-user desktop and Python installation. It checks exact baseline hashes, waits for active generation/research to finish, backs up the changed modules, original executable and databases, replaces only custom code/assets, retains the app identifier/WebView profile and data locations, and rolls back on replacement/startup failure. It does not reinstall models or replace authentication/settings databases. The packaged backend payload is applied to the installed 2026.10.5 baseline; frontend/native sources are based on upstream commit 9b30c4f75cb680e5fb7caa26a9602791a3bc9b28.

The development checkout and toolchain caches can be removed after installation and validation. The fork preserves recoverable source. Runtime Python files remain necessary to run Unsloth.

License: existing AGPL-3.0 terms apply to Studio and these modifications; see studio/LICENSE.AGPL-3.0.
