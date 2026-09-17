# Environment contract — bernstein

Keys only. Values are never read or emitted by agent-docs.

| Key | Secret? | Uses | Declared in | First use |
|---|---|---|---|---|
| `ACTIONS_ID_TOKEN_REQUEST_TOKEN` | yes | 1 | — | `src/bernstein/core/security/sigstore_attestation.py:232` |
| `ACTIONS_ID_TOKEN_REQUEST_URL` | yes | 2 | — | `src/bernstein/core/security/sigstore_attestation.py:225` |
| `ALLOWED_SCOPE` |  | 1 | — | `.github/workflows/bernstein-issues-decompose.yml:281` |
| `ANTHROPIC_API_KEY` | yes | 4 | `.env.example:15` | `src/bernstein/adapters/claude.py:1128` |
| `APPDATA` |  | 1 | — | `src/bernstein/core/substrate/host_registry.py:76` |
| `AWS_ACCESS_KEY_ID` |  | 3 | — | `src/bernstein/core/storage/sinks/r2.py:68` |
| `AWS_ENDPOINT_URL` |  | 3 | — | `src/bernstein/core/storage/sinks/s3.py:120` |
| `AWS_REGION` |  | 1 | — | `src/bernstein/core/storage/sinks/s3.py:119` |
| `AWS_SECRET_ACCESS_KEY` | yes | 2 | — | `src/bernstein/core/storage/sinks/r2.py:70` |
| `AWS_SESSION_TOKEN` | yes | 1 | — | `src/bernstein/core/storage/sinks/s3.py:125` |
| `AZURE_STORAGE_ACCOUNT_KEY` | yes | 1 | — | `tests/integration/storage/test_azure_blob_sink.py:52` |
| `AZURE_STORAGE_ACCOUNT_NAME` |  | 1 | — | `tests/integration/storage/test_azure_blob_sink.py:52` |
| `AZURE_STORAGE_CONNECTION_STRING` | yes | 1 | — | `tests/integration/storage/test_azure_blob_sink.py:51` |
| `BACKLOG_PATH` |  | 1 | — | `tests/integration/test_claim_next_concurrency.py:183` |
| `BAGGAGE` |  | 1 | — | `src/bernstein/adapters/base.py:1460` |
| `BEARTYPE_USE_CLAW` |  | 1 | — | `tests/_beartype_claw.py:30` |
| `BERNSTEIN_AB_TEST` |  | 1 | — | `src/bernstein/core/orchestration/orchestrator.py:7348` |
| `BERNSTEIN_ACCESSIBILITY` |  | 1 | — | `src/bernstein/tui/accessibility.py:90` |
| `BERNSTEIN_ACTIVITY_LOG` |  | 1 | — | `src/bernstein/cli/dashboard_app.py:297` |
| `BERNSTEIN_ADAPTER` |  | 4 | — | `src/bernstein/cli/run_bootstrap.py:2644` |
| `BERNSTEIN_ADAPTER_ADMISSION_POLICY` |  | 2 | — | `tests/conftest.py:592` |
| `BERNSTEIN_AGENT_CARD_KEY_DIR` |  | 2 | — | `src/bernstein/core/routes/well_known.py:191` |
| `BERNSTEIN_AGENT_IMAGE` |  | 1 | — | `src/bernstein/core/orchestration/operator.py:605` |
| `BERNSTEIN_ALLOW_HSM_STUB` |  | 1 | — | `src/bernstein/core/security/key_custody.py:466` |
| `BERNSTEIN_ALLOW_MERGE_TO_DEFAULT_BRANCH` |  | 2 | — | `tests/conftest.py:593` |
| `BERNSTEIN_AUDIT` |  | 1 | — | `src/bernstein/core/orchestration/orchestrator.py:1061` |
| `BERNSTEIN_AUDIT_KEY_PATH` |  | 4 | — | `tests/unit/test_replay_verify_cli.py:35` |
| `BERNSTEIN_AUDIT_MQTT_ENABLED` |  | 1 | — | `examples/plugins/custom-audit-sink/src/custom_audit_sink/_sink.py:58` |
| `BERNSTEIN_AUDIT_MQTT_TOPIC` |  | 1 | — | `examples/plugins/custom-audit-sink/src/custom_audit_sink/_sink.py:57` |
| `BERNSTEIN_AUDIT_MQTT_URL` |  | 1 | — | `examples/plugins/custom-audit-sink/src/custom_audit_sink/_sink.py:56` |
| `BERNSTEIN_AUDIT_RECEIPT_VERIFIER` |  | 1 | — | `src/bernstein/cli/commands/audit_cmd.py:3866` |
| `BERNSTEIN_AUTH_DISABLED` |  | 4 | — | `src/bernstein/core/server/server_launch.py:660` |
| `BERNSTEIN_AUTH_JWT_SECRET` | yes | 1 | — | `src/bernstein/core/identity/agent_jwt.py:598` |
| `BERNSTEIN_AUTH_SECRET` | yes | 1 | — | `src/bernstein/core/orchestration/operator.py:606` |
| `BERNSTEIN_AUTH_TOKEN` | yes | 4 | `.env.example:27` | `src/bernstein/cli/commands/auth_cmd.py:316` |
| `BERNSTEIN_AUTO_PR` |  | 1 | — | `src/bernstein/core/orchestration/orchestrator.py:5750` |
| `BERNSTEIN_AZURE_CONTAINER` |  | 1 | — | `src/bernstein/core/storage/sinks/azure_blob.py:91` |
| `BERNSTEIN_BIN` |  | 1 | — | `src/bernstein/core/fleet/bulk.py:148` |
| `BERNSTEIN_BIND_HOST` |  | 4 | — | `src/bernstein/cli/run_bootstrap.py:3028` |
| `BERNSTEIN_BROKER_KEY` | yes | 1 | — | `src/bernstein/core/security/secrets_broker.py:451` |
| `BERNSTEIN_CLI` |  | 2 | — | `tests/conftest.py:589` |
| `BERNSTEIN_CLUSTER_AUTH_SECRET` | yes | 1 | — | `src/bernstein/core/server/server_app.py:1632` |
| `BERNSTEIN_CLUSTER_ENABLED` |  | 2 | — | `src/bernstein/core/orchestration/orchestrator.py:7322` |
| `BERNSTEIN_COMPLIANCE` |  | 3 | — | `src/bernstein/core/orchestration/bootstrap.py:364` |
| `BERNSTEIN_CONTAINER` |  | 4 | — | `src/bernstein/cli/run_bootstrap.py:407` |
| `BERNSTEIN_CONTAINER_IMAGE` |  | 1 | — | `src/bernstein/core/orchestration/orchestrator.py:7026` |
| `BERNSTEIN_COST_USAGE_BUFFER` |  | 1 | — | `src/bernstein/core/cost/cost_tracker.py:171` |
| `BERNSTEIN_DAILY_BUDGET_USD` |  | 1 | — | `examples/plugins/custom_router_plugin.py:93` |
| `BERNSTEIN_DASHBOARD_PASSWORD` | yes | 2 | — | `src/bernstein/core/server/dashboard_auth.py:234` |
| `BERNSTEIN_DATABASE_URL` | yes | 3 | — | `src/bernstein/cli/commands/status_cmd.py:548` |
| `BERNSTEIN_DEBUG` |  | 1 | — | `src/bernstein/core/server/server_middleware.py:225` |
| `BERNSTEIN_DEBUG_SPLASH` |  | 1 | — | `src/bernstein/cli/display/splash_screen.py:712` |
| `BERNSTEIN_DEFAULT_CLI` |  | 1 | — | `src/bernstein/cli/commands/chat_cmd.py:611` |
| `BERNSTEIN_DETERMINISTIC_SEED` |  | 1 | — | `src/bernstein/core/orchestration/orchestrator.py:6715` |
| `BERNSTEIN_FAKE_CLI_ARGV_DUMP` |  | 1 | — | `tests/integration/fake_cli/fake_cli.py:389` |
| `BERNSTEIN_FAKE_CLI_CONFIG` |  | 1 | — | `tests/integration/fake_cli/fake_cli.py:479` |
| `BERNSTEIN_FAKE_CLI_DELAY_S` |  | 1 | — | `tests/integration/fake_cli/fake_cli.py:509` |
| `BERNSTEIN_FAKE_CLI_ENV_DUMP` |  | 1 | — | `tests/integration/fake_cli/fake_cli.py:375` |
| `BERNSTEIN_FAKE_CLI_EXIT_CODE` |  | 1 | — | `tests/integration/fake_cli/fake_cli.py:505` |
| `BERNSTEIN_FAKE_CLI_MODE` |  | 1 | — | `tests/integration/fake_cli/fake_cli.py:503` |
| `BERNSTEIN_FAKE_CLI_PROFILE` |  | 1 | — | `tests/integration/fake_cli/fake_cli.py:351` |
| `BERNSTEIN_FAKE_CLI_STDERR` |  | 1 | — | `tests/integration/fake_cli/fake_cli.py:424` |
| `BERNSTEIN_FAKE_CLI_STDOUT` |  | 1 | — | `tests/integration/fake_cli/fake_cli.py:409` |
| `BERNSTEIN_FLEET_CONFIG` |  | 1 | — | `src/bernstein/core/fleet/config.py:99` |
| `BERNSTEIN_FLEET_ROOT` |  | 1 | — | `src/bernstein/core/fleet/directory_registry.py:96` |
| `BERNSTEIN_FORCE_OPUS` |  | 1 | — | `tests/integration/test_criterion_profile_routing.py:262` |
| `BERNSTEIN_FRAME_ANCESTORS` |  | 1 | — | `src/bernstein/core/server/frame_headers.py:31` |
| `BERNSTEIN_GATE_REPAIR` |  | 1 | — | `src/bernstein/core/tasks/task_lifecycle.py:3196` |
| `BERNSTEIN_GCS_BUCKET` |  | 1 | — | `src/bernstein/core/storage/sinks/gcs.py:85` |
| `BERNSTEIN_GCS_TEST_BUCKET` |  | 2 | — | `tests/integration/storage/test_gcs_sink.py:47` |
| `BERNSTEIN_GITHUB_PAGE_LIMIT` |  | 1 | — | `src/bernstein/core/git/github.py:97` |
| `BERNSTEIN_GITLAB_URL` |  | 1 | — | `src/bernstein/gitlab_app/app.py:94` |
| `BERNSTEIN_HARD_BUDGET_USD` |  | 2 | — | `src/bernstein/cli/run_bootstrap.py:388` |
| `BERNSTEIN_HEARTBEAT_TIMEOUT` |  | 2 | — | `tests/conftest.py:591` |
| `BERNSTEIN_JANITOR_FUZZY_PATHS` |  | 1 | — | `src/bernstein/core/quality/janitor.py:1874` |
| `BERNSTEIN_JANITOR_REOPEN_MAX` |  | 1 | — | `src/bernstein/core/tasks/task_lifecycle.py:5135` |
| `BERNSTEIN_LINEAGE_OP_SECRET` | yes | 2 | — | `src/bernstein/cli/commands/audit_cmd.py:3245` |
| `BERNSTEIN_LOG_JSON` |  | 1 | — | `src/bernstein/core/server/json_logging.py:104` |
| `BERNSTEIN_MAX_BLAST_RADIUS` |  | 1 | — | `src/bernstein/cli/run_bootstrap.py:395` |
| `BERNSTEIN_MAX_TASK_RETRIES` |  | 2 | — | `tests/conftest.py:590` |
| `BERNSTEIN_MCP_CATALOG_AUDIT_DIR` |  | 1 | — | `src/bernstein/cli/commands/mcp_catalog_cmd.py:51` |
| `BERNSTEIN_MCP_CATALOG_CACHE_PATH` |  | 2 | — | `src/bernstein/cli/commands/mcp_catalog_cmd.py:78` |
| `BERNSTEIN_MCP_CATALOG_CHECK_INTERVAL` |  | 1 | — | `src/bernstein/cli/commands/mcp_catalog_cmd.py:59` |
| `BERNSTEIN_MCP_CATALOG_REVALIDATE_INTERVAL` |  | 1 | — | `src/bernstein/cli/commands/mcp_catalog_cmd.py:68` |
| `BERNSTEIN_MCP_USER_CONFIG_PATH` |  | 2 | — | `src/bernstein/cli/commands/mcp_catalog_cmd.py:83` |
| `BERNSTEIN_MCP_VALIDATION` |  | 4 | — | `tests/unit/test_mcp_input_validation.py:51` |
| `BERNSTEIN_MEM_GUARD_GB` |  | 1 | — | `tests/conftest.py:70` |
| `BERNSTEIN_METRICS_DIR` |  | 2 | — | `docs/integrations/plugin-sdk.md:531` |
| `BERNSTEIN_MICROVM_INTEGRATION` |  | 1 | — | `tests/integration/sandbox/test_microvm_firecracker.py:46` |
| `BERNSTEIN_MOCK_IDLE` |  | 2 | — | `src/bernstein/adapters/mock.py:345` |
| `BERNSTEIN_MODEL` |  | 2 | — | `src/bernstein/core/orchestration/orchestrator.py:6659` |
| `BERNSTEIN_NERD_FONT` |  | 1 | — | `src/bernstein/core/observability/icons.py:163` |
| `BERNSTEIN_NO_TUI` |  | 1 | — | `src/bernstein/cli/display/terminal_caps.py:180` |
| `BERNSTEIN_OPERATOR_ID` |  | 1 | — | `src/bernstein/core/orchestration/consensus_relay.py:422` |
| `BERNSTEIN_ORCHESTRATION_RELAY_PATH` |  | 1 | — | `src/bernstein/core/orchestration/consensus_relay.py:511` |
| `BERNSTEIN_ORG` |  | 1 | — | `src/bernstein/core/security/hipaa.py:585` |
| `BERNSTEIN_OUTPUT` |  | 1 | — | `src/bernstein/cli/run.py:63` |
| `BERNSTEIN_PLUGIN_DATA_ROOT` |  | 1 | — | `src/bernstein/core/protocols/mcp/agent_plugins_mcp_register.py:44` |
| `BERNSTEIN_PREVIEW_SECRET` | yes | 1 | — | `src/bernstein/core/preview/manager.py:794` |
| `BERNSTEIN_PROFILE` |  | 2 | — | `src/bernstein/core/observability/profiler.py:14` |
| `BERNSTEIN_PROFILE_OUTPUT` |  | 3 | — | `src/bernstein/core/observability/profiler.py:294` |
| `BERNSTEIN_PROMETHEUS_IMPORT_TIMEOUT` |  | 1 | — | `src/bernstein/core/observability/prometheus.py:31` |
| `BERNSTEIN_PROMPT_MAX_PATCHES_PER_SESSION` |  | 1 | — | `src/bernstein/evolution/oscillation_guard.py:129` |
| `BERNSTEIN_PROMPT_MIN_DELTA` |  | 1 | — | `src/bernstein/evolution/predicted_delta.py:215` |
| `BERNSTEIN_PROVIDER_KEYS_SECRET` | yes | 1 | — | `src/bernstein/core/orchestration/operator.py:607` |
| `BERNSTEIN_PUBLIC_BASE_URL` |  | 1 | — | `src/bernstein/core/routes/well_known.py:526` |
| `BERNSTEIN_QUIESCENCE_SETTLE_S` |  | 2 | — | `src/bernstein/core/orchestration/orchestrator.py:2533` |
| `BERNSTEIN_QUIET` |  | 2 | — | `src/bernstein/core/orchestration/orchestrator.py:6043` |
| `BERNSTEIN_R2_BUCKET` |  | 1 | — | `src/bernstein/core/storage/sinks/r2.py:66` |
| `BERNSTEIN_R2_TEST_BUCKET` |  | 2 | — | `tests/integration/storage/test_r2_sink.py:41` |
| `BERNSTEIN_READONLY` |  | 1 | — | `src/bernstein/core/server/server_app.py:1647` |
| `BERNSTEIN_REDIS_URL` |  | 2 | — | `src/bernstein/cli/commands/status_cmd.py:588` |
| `BERNSTEIN_REFRESH_CACHE` |  | 1 | — | `src/bernstein/cli/run_bootstrap.py:2564` |
| `BERNSTEIN_RELAY_KEY` | yes | 1 | — | `src/bernstein/core/orchestration/consensus_relay.py:443` |
| `BERNSTEIN_REMOTE_QUICKSTART` |  | 2 | — | `src/bernstein/cli/commands/status_cmd.py:1259` |
| `BERNSTEIN_REPLAY_RUN_ID` |  | 1 | — | `src/bernstein/core/orchestration/orchestrator.py:6727` |
| `BERNSTEIN_REQUEST_LOG_LEVEL` |  | 1 | — | `src/bernstein/core/server/request_logging.py:47` |
| `BERNSTEIN_RETRY_BUDGET_SPEC` |  | 1 | — | `src/bernstein/cli/run_bootstrap.py:2558` |
| `BERNSTEIN_REVIEW_AUTH_TOKEN` | yes | 1 | — | `src/bernstein/core/volunteer/review_task.py:75` |
| `BERNSTEIN_REVIEW_SERVER_URL` |  | 1 | — | `src/bernstein/core/volunteer/review_task.py:50` |
| `BERNSTEIN_ROUTING` |  | 1 | — | `src/bernstein/core/orchestration/orchestrator.py:612` |
| `BERNSTEIN_RUN_ATTACHMENTS` |  | 1 | — | `src/bernstein/cli/run_bootstrap.py:2535` |
| `BERNSTEIN_RUN_CLUSTER_E2E` |  | 1 | — | `tests/integration/cluster/conftest.py:648` |
| `BERNSTEIN_RUN_CRITERION_PROFILE` |  | 1 | — | `src/bernstein/cli/run_bootstrap.py:2506` |
| `BERNSTEIN_RUN_ID` |  | 4 | — | `src/bernstein/adapters/openai_agents_runner.py:1828` |
| `BERNSTEIN_S3_BUCKET` |  | 1 | — | `src/bernstein/core/storage/sinks/s3.py:117` |
| `BERNSTEIN_S3_TEST_BUCKET` |  | 1 | — | `tests/integration/storage/test_s3_sink.py:70` |
| `BERNSTEIN_SANDBOX` |  | 4 | — | `src/bernstein/core/security/guardrails.py:65` |
| `BERNSTEIN_SANDBOX_ALLOW_PAID` |  | 2 | — | `tests/unit/test_cli_run_sandbox.py:159` |
| `BERNSTEIN_SANDBOX_RUNTIME` |  | 4 | — | `src/bernstein/cli/run_bootstrap.py:408` |
| `BERNSTEIN_SBOM_ON_COMPLETE` |  | 1 | — | `src/bernstein/core/routes/task_crud.py:833` |
| `BERNSTEIN_SDD_DIR` |  | 3 | — | `scripts/auto_heal_v2_run.py:55` |
| `BERNSTEIN_SECURITY_CMD` |  | 2 | — | `docs/integrations/plugin-sdk.md:631` |
| `BERNSTEIN_SEED_PATH` |  | 4 | — | `src/bernstein/core/config/seed.py:435` |
| `BERNSTEIN_SERVER_URL` |  | 4 | — | `src/bernstein/adapters/claude.py:786` |
| `BERNSTEIN_SKILLS_CATALOG_AUDIT_DIR` |  | 1 | — | `src/bernstein/cli/commands/skills_catalog_cmd.py:51` |
| `BERNSTEIN_SKILLS_CATALOG_CACHE_PATH` |  | 1 | — | `src/bernstein/cli/commands/skills_catalog_cmd.py:59` |
| `BERNSTEIN_SKIP_AZURE_TESTS` |  | 1 | — | `tests/integration/storage/test_azure_blob_sink.py:48` |
| `BERNSTEIN_SKIP_DOCKER_TESTS` |  | 1 | — | `tests/integration/sandbox/test_docker_backend.py:33` |
| `BERNSTEIN_SKIP_GATES` |  | 2 | — | `src/bernstein/cli/run_preflight.py:854` |
| `BERNSTEIN_SKIP_GATE_REASON` |  | 2 | — | `src/bernstein/cli/run_preflight.py:856` |
| `BERNSTEIN_SKIP_GCS_TESTS` |  | 1 | — | `tests/integration/storage/test_gcs_sink.py:42` |
| `BERNSTEIN_SKIP_R2_TESTS` |  | 1 | — | `tests/integration/storage/test_r2_sink.py:35` |
| `BERNSTEIN_SKIP_S3_TESTS` |  | 1 | — | `tests/integration/storage/test_s3_sink.py:42` |
| `BERNSTEIN_SLACK_APP_TOKEN` | yes | 1 | — | `src/bernstein/cli/commands/mission_cmd.py:397` |
| `BERNSTEIN_STATE_KEY_PASSPHRASE` |  | 1 | — | `src/bernstein/core/security/state_encryption.py:402` |
| `BERNSTEIN_STATE_KEY_PATH` |  | 1 | — | `src/bernstein/core/security/state_encryption.py:370` |
| `BERNSTEIN_STORAGE_BACKEND` |  | 2 | — | `src/bernstein/cli/commands/status_cmd.py:528` |
| `BERNSTEIN_TASK_FILTER` |  | 4 | — | `src/bernstein/core/git/github.py:1066` |
| `BERNSTEIN_TASK_ID` |  | 1 | — | `src/bernstein/cli/commands/memory_cmd.py:48` |
| `BERNSTEIN_TELEMETRY_DSN` | yes | 1 | — | `src/bernstein/cli/main.py:377` |
| `BERNSTEIN_TEST_API_KEY` | yes | 1 | — | `tests/integration/test_stack_integrations.py:30` |
| `BERNSTEIN_THEME` |  | 1 | — | `src/bernstein/tui/themes.py:145` |
| `BERNSTEIN_TRACES_DIR` |  | 1 | — | `src/bernstein/cli/commands/compare_cmd.py:39` |
| `BERNSTEIN_TREND_SCAN_OFFLINE` |  | 1 | — | `src/bernstein/cli/commands/trend_scan_cmd.py:231` |
| `BERNSTEIN_TWO_PHASE_SANDBOX` |  | 1 | — | `src/bernstein/core/orchestration/orchestrator.py:7027` |
| `BERNSTEIN_UNSAFE_ALLOW_UNICODE_TAGS` |  | 1 | — | `src/bernstein/cli/main.py:781` |
| `BERNSTEIN_URL` |  | 2 | — | `integrations/jira_webhook/app.py:92` |
| `BERNSTEIN_WORKFLOW` |  | 1 | — | `src/bernstein/core/orchestration/orchestrator.py:7285` |
| `BLAXEL_API_KEY` | yes | 1 | — | `tests/integration/sandbox/test_blaxel_backend.py:29` |
| `BLAXEL_API_URL` |  | 1 | — | `src/bernstein/core/sandbox/backends/blaxel.py:284` |
| `BLAXEL_WORKSPACE` |  | 1 | — | `tests/integration/sandbox/test_blaxel_backend.py:29` |
| `BRIDGE_API_KEY` | yes | 1 | — | `docs/cloudflare/cloudflare-codex-sandbox.md:223` |
| `BRIDGE_URL` |  | 1 | — | `docs/cloudflare/cloudflare-codex-sandbox.md:222` |
| `CF_TUNNEL_HOSTNAME` |  | 1 | — | `tests/integration/cluster/test_cluster_tunnel_smoke.py:48` |
| `CF_TUNNEL_TOKEN` | yes | 1 | — | `tests/integration/cluster/test_cluster_tunnel_smoke.py:47` |
| `CHANGED_FILES` |  | 1 | — | `.github/workflows/contract-drift-autofix.yml:450` |
| `CI_BLAXEL_TEST` |  | 1 | — | `tests/integration/sandbox/test_blaxel_backend.py:27` |
| `CI_DAYTONA_TEST` |  | 1 | — | `tests/integration/sandbox/test_daytona_backend.py:21` |
| `CI_RUNLOOP_TEST` |  | 1 | — | `tests/integration/sandbox/test_runloop_backend.py:20` |
| `CI_TUNNEL_TEST` |  | 1 | — | `tests/integration/cluster/test_cluster_tunnel_smoke.py:46` |
| `CI_VERCEL_TEST` |  | 1 | — | `tests/integration/sandbox/test_vercel_backend.py:20` |
| `CLAIMER_ID` |  | 1 | — | `tests/integration/test_claim_next_concurrency.py:184` |
| `CLAIM_ROLE` |  | 1 | — | `tests/integration/test_claim_next_concurrency.py:185` |
| `CLOUDFLARE_ACCOUNT_ID` |  | 2 | — | `docs/cloudflare/cloudflare-browser-rendering.md:50` |
| `CLOUDFLARE_API_TOKEN` | yes | 2 | — | `docs/cloudflare/cloudflare-browser-rendering.md:51` |
| `CODESPACES` |  | 2 | — | `src/bernstein/cli/commands/status_cmd.py:1257` |
| `COLORFGBG` |  | 1 | — | `src/bernstein/tui/themes.py:154` |
| `COLORTERM` |  | 2 | — | `src/bernstein/cli/display/terminal_caps.py:106` |
| `COMSPEC` |  | 1 | — | `src/bernstein/core/orchestration/worker.py:117` |
| `CURSOR_API_KEY` | yes | 1 | — | `src/bernstein/adapters/cursor.py:205` |
| `DATADOG_API_KEY` | yes | 3 | `.env.example:43` | `src/bernstein/core/observability/apm_integration.py:123` |
| `DAYTONA_API_KEY` | yes | 1 | — | `tests/integration/sandbox/test_daytona_backend.py:23` |
| `DAYTONA_API_URL` |  | 2 | — | `src/bernstein/core/sandbox/backends/daytona.py:298` |
| `DAYTONA_ORG_ID` |  | 2 | — | `src/bernstein/core/sandbox/backends/daytona.py:300` |
| `DAYTONA_TARGET` |  | 1 | — | `src/bernstein/core/sandbox/backends/daytona.py:299` |
| `DD_AGENT_HOST` |  | 2 | — | `src/bernstein/core/observability/apm_integration.py:130` |
| `DD_API_KEY` | yes | 3 | — | `src/bernstein/core/observability/apm_integration.py:123` |
| `DD_ENV` |  | 1 | — | `src/bernstein/core/observability/apm_integration.py:128` |
| `DD_SERVICE` |  | 1 | — | `src/bernstein/core/observability/apm_integration.py:127` |
| `DD_SITE` |  | 1 | `.env.example:44` | `src/bernstein/core/observability/apm_integration.py:126` |
| `DD_TAGS` |  | 1 | — | `src/bernstein/core/observability/apm_integration.py:257` |
| `DD_TRACE_AGENT_PORT` |  | 1 | — | `src/bernstein/core/observability/apm_integration.py:131` |
| `DD_VERSION` |  | 1 | — | `src/bernstein/core/observability/apm_integration.py:129` |
| `DEVIN_API_KEY` | yes | 1 | — | `src/bernstein/adapters/devin_terminal.py:135` |
| `DISCORD_PUBLIC_KEY` | yes | 1 | — | `src/bernstein/core/routes/discord.py:62` |
| `DISCORD_WEBHOOK_URL` |  | 2 | — | `docs/integrations/plugin-sdk.md:416` |
| `E2B_API_KEY` | yes | 1 | — | `tests/integration/sandbox/test_e2b_backend.py:26` |
| `EVENT_NAME` |  | 1 | — | `.github/workflows/ci.yml:2388` |
| `GEMINI_API_KEY` | yes | 2 | — | `src/bernstein/adapters/gemini.py:243` |

> Showing 200 of 350; the rest is in `.machine/facts.json`.

> 337 key(s) are read by code but absent from any `.env.example`-style file. Listed above with *Declared in* = —.
