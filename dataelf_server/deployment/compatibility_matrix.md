> 此表记录 LLM 意图集成之前的历史迁移。旧规则解析现已归档停用；当前行为见 deployment/README.md，新增测试见 test_intent.py 和 test_intent_integration.py。

# 旧服务兼容测试映射

旧目录基线重新执行：171 passed。下表逐项列出原测试函数；参数化测试共享函数条目。新服务测试不通过旧目录的 PYTHONPATH 运行。

| 原测试函数 | 处理 | 新覆盖 |
|---|---|---|
| `test_ai_index.py::test_news_client_sends_one_authenticated_request` | 适配 | `test_scope_v2.py` |
| `test_ai_index.py::test_news_client_retries_429_with_exponential_backoff` | 适配 | `test_scope_v2.py` |
| `test_ai_index.py::test_news_client_rejects_business_error` | 适配 | `test_scope_v2.py` |
| `test_ai_index.py::test_news_client_rejects_non_retryable_http_error` | 适配 | `test_scope_v2.py` |
| `test_ai_index.py::test_news_client_rejects_malformed_success_response` | 适配 | `test_scope_v2.py` |
| `test_api.py::test_app_exposes_only_public_job_routes` | 保留并适配 | `test_api.py::test_app_exposes_only_public_job_routes` |
| `test_api.py::test_submit_uses_natural_language_body` | 保留并适配 | `test_api.py::test_submit_uses_natural_language_body` |
| `test_api.py::test_submit_defaults_to_scope_v2` | 保留并适配 | `test_api.py::test_submit_defaults_to_scope_v2` |
| `test_api.py::test_validation_errors_use_standard_envelope` | 保留并适配 | `test_api.py::test_validation_errors_use_standard_envelope` |
| `test_api.py::test_status_unknown_and_not_ready_result` | 保留并适配 | `test_api.py::test_status_unknown_and_not_ready_result` |
| `test_api.py::test_status_returns_created_and_started_times` | 保留并适配 | `test_api.py::test_status_returns_created_and_started_times` |
| `test_api.py::test_retry_reuses_job_id_and_rejects_non_failed_jobs` | 保留并适配 | `test_api.py::test_retry_reuses_job_id_and_rejects_non_failed_jobs` |
| `test_api.py::test_completed_and_failed_results` | 保留并适配 | `test_api.py::test_completed_and_failed_results` |
| `test_api_entry.py::test_validate_insights_accepts_any_positive_count` | 适配 | `test_api.py; test_workflow.py; test_settings_and_distribution.py` |
| `test_api_entry.py::test_validate_insights_rejects_empty_or_incomplete_values` | 适配 | `test_api.py; test_workflow.py; test_settings_and_distribution.py` |
| `test_api_entry.py::test_parse_args_accepts_custom_request` | 适配 | `test_api.py; test_workflow.py; test_settings_and_distribution.py` |
| `test_api_entry.py::test_run_test_uses_all_three_public_endpoints` | 适配 | `test_api.py; test_workflow.py; test_settings_and_distribution.py` |
| `test_errors.py::test_classifies_public_job_errors` | 保留并适配 | `test_errors.py::test_classifies_public_job_errors` |
| `test_errors.py::test_error_contract_does_not_expose_internal_message` | 保留并适配 | `test_errors.py::test_error_contract_does_not_expose_internal_message` |
| `test_errors.py::test_internal_transport_error_uses_failure_stage` | 保留并适配 | `test_errors.py::test_internal_transport_error_uses_failure_stage` |
| `test_insight_contract.py::test_comprehensive_contract_renders_common_and_module_rules` | 保留并适配 | `test_insight_contract.py::test_comprehensive_contract_renders_common_and_module_rules` |
| `test_insight_contract.py::test_multi_module_contract_keeps_module_rules_separate` | 保留并适配 | `test_insight_contract.py::test_multi_module_contract_keeps_module_rules_separate` |
| `test_insight_contract.py::test_comprehensive_validator_accepts_contract_compliant_chinese` | 保留并适配 | `test_insight_contract.py::test_comprehensive_validator_accepts_contract_compliant_chinese` |
| `test_insight_contract.py::test_validator_rejects_language_boilerplate_and_markdown_but_not_length` | 保留并适配 | `test_insight_contract.py::test_validator_rejects_language_boilerplate_and_markdown_but_not_length` |
| `test_insight_contract.py::test_non_comprehensive_module_has_no_content_length_requirement` | 保留并适配 | `test_insight_contract.py::test_non_comprehensive_module_has_no_content_length_requirement` |
| `test_insight_contract.py::test_title_and_content_lengths_are_prompt_guidance_not_a_hard_gate` | 保留并适配 | `test_insight_contract.py::test_title_and_content_lengths_are_prompt_guidance_not_a_hard_gate` |
| `test_insight_contract.py::test_workspace_validator_reads_persisted_contract` | 保留并适配 | `test_insight_contract.py::test_workspace_validator_reads_persisted_contract` |
| `test_insight_contract.py::test_workspace_validator_rejects_unsupported_numbers_and_proper_nouns` | 保留并适配 | `test_insight_contract.py::test_workspace_validator_rejects_unsupported_numbers_and_proper_nouns` |
| `test_insight_contract.py::test_source_grounding_ignores_whitespace_inside_latin_name` | 保留并适配 | `test_insight_contract.py::test_source_grounding_ignores_whitespace_inside_latin_name` |
| `test_insight_contract.py::test_source_grounding_accepts_equivalent_numeric_units_and_name_formatting` | 保留并适配 | `test_insight_contract.py::test_source_grounding_accepts_equivalent_numeric_units_and_name_formatting` |
| `test_manager.py::test_manager_runs_job_and_persists_result` | 保留并适配 | `test_manager.py::test_manager_runs_job_and_persists_result` |
| `test_manager.py::test_manager_records_pipeline_failure` | 保留并适配 | `test_manager.py::test_manager_records_pipeline_failure` |
| `test_manager.py::test_store_marks_incomplete_jobs_failed_after_restart` | 保留并适配 | `test_manager.py::test_store_marks_incomplete_jobs_failed_after_restart` |
| `test_manager.py::test_store_migrates_started_at_and_sets_it_only_once` | 保留并适配 | `test_manager.py::test_store_migrates_started_at_and_sets_it_only_once` |
| `test_manager.py::test_manager_retry_archives_each_attempt_and_reuses_request` | 保留并适配 | `test_manager.py::test_manager_retry_archives_each_attempt_and_reuses_request` |
| `test_manager.py::test_manager_retry_rejects_active_job_and_rolls_back_archive` | 保留并适配 | `test_manager.py::test_manager_retry_rejects_active_job_and_rolls_back_archive` |
| `test_manager.py::test_manager_recovers_historical_retry_parser_failure` | 保留并适配 | `test_manager.py::test_manager_recovers_historical_retry_parser_failure` |
| `test_manager.py::test_manager_immediate_close_cancels_active_job` | 保留并适配 | `test_manager.py::test_manager_immediate_close_cancels_active_job` |
| `test_pi_convergence.py::test_pi_ontology_passes_rdf_runtime_flag` | 适配 | `test_workflow.py; test_pi_integration.py` |
| `test_pi_convergence.py::test_pi_runs_one_bounded_synthesis_retry` | 适配 | `test_workflow.py; test_pi_integration.py` |
| `test_pi_convergence.py::test_pi_timeout_runs_synthesis_only_recovery` | 适配 | `test_workflow.py; test_pi_integration.py` |
| `test_pi_convergence.py::test_pi_runs_one_format_only_retry_for_contract_violations` | 适配 | `test_workflow.py; test_pi_integration.py` |
| `test_pi_environment.py::test_pi_subprocess_receives_ai_index_credentials_for_dynamic_discovery` | 适配 | `test_settings_and_distribution.py; test_lifecycle_and_runtime.py` |
| `test_pi_environment.py::test_pi_event_stream_reports_model_errors` | 适配 | `test_settings_and_distribution.py; test_lifecycle_and_runtime.py` |
| `test_pi_environment.py::test_pi_event_stream_ignores_model_error_after_successful_retry` | 适配 | `test_settings_and_distribution.py; test_lifecycle_and_runtime.py` |
| `test_pi_environment.py::test_pi_event_stream_keeps_final_failed_retry` | 适配 | `test_settings_and_distribution.py; test_lifecycle_and_runtime.py` |
| `test_pi_environment.py::test_pi_command_runs_original_dataelf_agent_task` | 适配 | `test_settings_and_distribution.py; test_lifecycle_and_runtime.py` |
| `test_pi_extension_contract.py::test_finalizer_requires_verified_analysis_instead_of_call_count` | 适配 | `test_pi_integration.py; test_rdf_finalize.py` |
| `test_pi_extension_contract.py::test_failed_analysis_submission_is_bounded_and_uses_the_same_tool` | 适配 | `test_pi_integration.py; test_rdf_finalize.py` |
| `test_pi_extension_contract.py::test_contract_issues_receive_bounded_in_place_finalizer_retries` | 适配 | `test_pi_integration.py; test_rdf_finalize.py` |
| `test_pi_extension_contract.py::test_finalizer_only_advertises_candidate_signal_sources` | 适配 | `test_pi_integration.py; test_rdf_finalize.py` |
| `test_pi_response.py::test_materialize_pi_response_accepts_variable_counts` | 按计划移除 | `test_workflow.py::test_stdout_payload_cannot_complete_a_server_job` |
| `test_pi_response.py::test_materialize_pi_response_rejects_empty_or_unsafe_values` | 按计划移除 | `test_workflow.py::test_stdout_payload_cannot_complete_a_server_job` |
| `test_pipeline.py::test_pipeline_passes_exact_natural_language_query_to_dataelf` | 适配 | `test_workflow.py; test_lifecycle_and_runtime.py` |
| `test_pipeline.py::test_pipeline_dispatches_pi_ontology_and_preserves_scope` | 适配 | `test_workflow.py; test_lifecycle_and_runtime.py` |
| `test_pipeline.py::test_pipeline_always_uses_pi_ontology` | 适配 | `test_workflow.py; test_lifecycle_and_runtime.py` |
| `test_pipeline.py::test_pipeline_rejects_empty_query` | 适配 | `test_workflow.py; test_lifecycle_and_runtime.py` |
| `test_pipeline.py::test_pipeline_requires_real_nonempty_ai_index_source` | 适配 | `test_workflow.py; test_lifecycle_and_runtime.py` |
| `test_pipeline.py::test_pipeline_accepts_any_positive_insight_count` | 适配 | `test_workflow.py; test_lifecycle_and_runtime.py` |
| `test_pipeline.py::test_pipeline_rejects_zero_insights` | 适配 | `test_workflow.py; test_lifecycle_and_runtime.py` |
| `test_pipeline.py::test_pipeline_rejects_unsafe_insight_ids` | 适配 | `test_workflow.py; test_lifecycle_and_runtime.py` |
| `test_pipeline.py::test_pipeline_rejects_duplicate_insight_ids` | 适配 | `test_workflow.py; test_lifecycle_and_runtime.py` |
| `test_pipeline.py::test_pipeline_sets_project_local_pi_model_registry` | 适配 | `test_workflow.py; test_lifecycle_and_runtime.py` |
| `test_pipeline.py::test_pipeline_defaults_to_scope_v2_and_pi_ontology` | 适配 | `test_workflow.py; test_lifecycle_and_runtime.py` |
| `test_pipeline.py::test_pipeline_rejects_time_ranges_before_calling_ai_index` | 适配 | `test_workflow.py; test_lifecycle_and_runtime.py` |
| `test_pipeline.py::test_pipeline_supports_scope_v2_with_pi_ontology` | 适配 | `test_workflow.py; test_lifecycle_and_runtime.py` |
| `test_pipeline.py::test_pipeline_builds_real_pi_ontology_adapter` | 适配 | `test_workflow.py; test_lifecycle_and_runtime.py` |
| `test_presentation.py::test_public_insight_resolves_sources_for_both_explorers` | 保留并适配 | `test_presentation.py::test_public_insight_resolves_sources_for_both_explorers` |
| `test_presentation.py::test_public_insight_prefers_embedded_source_and_removes_duplicate_urls` | 保留并适配 | `test_presentation.py::test_public_insight_prefers_embedded_source_and_removes_duplicate_urls` |
| `test_process_control.py::test_terminate_active_process_group` | 适配 | `test_lifecycle_and_runtime.py` |
| `test_rdf_finalize.py::test_finalizer_rejects_unverified_analysis` | 保留并适配 | `test_rdf_finalize.py::test_finalizer_rejects_unverified_analysis` |
| `test_rdf_finalize.py::test_analysis_manifest_rejects_script_tampering` | 保留并适配 | `test_rdf_finalize.py::test_analysis_manifest_rejects_script_tampering` |
| `test_rdf_finalize.py::test_analysis_normalizes_mechanical_artifact_failures` | 保留并适配 | `test_rdf_finalize.py::test_analysis_normalizes_mechanical_artifact_failures` |
| `test_rdf_finalize.py::test_analysis_rebuilds_empty_source_table_from_cited_rdf_sources` | 保留并适配 | `test_rdf_finalize.py::test_analysis_rebuilds_empty_source_table_from_cited_rdf_sources` |
| `test_rdf_finalize.py::test_finalizer_only_writes_results_after_verified_analysis` | 保留并适配 | `test_rdf_finalize.py::test_finalizer_only_writes_results_after_verified_analysis` |
| `test_rdf_finalize.py::test_finalizer_repairs_omitted_proving_signal_and_rejects_unproven_source` | 保留并适配 | `test_rdf_finalize.py::test_finalizer_repairs_omitted_proving_signal_and_rejects_unproven_source` |
| `test_rdf_finalize.py::test_finalizer_does_not_hard_reject_title_or_thesis_length_or_truncate` | 保留并适配 | `test_rdf_finalize.py::test_finalizer_does_not_hard_reject_title_or_thesis_length_or_truncate` |
| `test_rdf_finalize.py::test_finalizer_drops_only_contract_invalid_insights` | 保留并适配 | `test_rdf_finalize.py::test_finalizer_drops_only_contract_invalid_insights` |
| `test_rdf_finalize.py::test_finalizer_keeps_retry_feedback_when_all_insights_are_invalid` | 保留并适配 | `test_rdf_finalize.py::test_finalizer_keeps_retry_feedback_when_all_insights_are_invalid` |
| `test_rdf_finalize.py::test_finalizer_resolves_unambiguous_model_source_aliases` | 保留并适配 | `test_rdf_finalize.py::test_finalizer_resolves_unambiguous_model_source_aliases` |
| `test_redaction.py::test_error_redaction_removes_known_key_shapes` | 保留并适配 | `test_redaction.py::test_error_redaction_removes_known_key_shapes` |
| `test_schemas.py::test_natural_language_query_is_trimmed` | 保留并适配 | `test_schemas.py::test_natural_language_query_is_trimmed` |
| `test_schemas.py::test_query_request_rejects_explorer_selection` | 保留并适配 | `test_schemas.py::test_query_request_rejects_explorer_selection` |
| `test_schemas.py::test_query_request_accepts_both_scopes` | 保留并适配 | `test_schemas.py::test_query_request_accepts_both_scopes` |
| `test_schemas.py::test_query_request_rejects_invalid_values` | 保留并适配 | `test_schemas.py::test_query_request_rejects_invalid_values` |
| `test_scope_v2.py::test_comprehensive_aliases_create_daily_plan` | 保留并适配 | `test_scope_v2.py::test_comprehensive_aliases_create_daily_plan` |
| `test_scope_v2.py::test_comprehensive_takes_precedence_over_modules` | 保留并适配 | `test_scope_v2.py::test_comprehensive_takes_precedence_over_modules` |
| `test_scope_v2.py::test_module_aliases` | 保留并适配 | `test_scope_v2.py::test_module_aliases` |
| `test_scope_v2.py::test_multi_module_plan_deduplicates_sources_and_sets_exact_payloads` | 保留并适配 | `test_scope_v2.py::test_multi_module_plan_deduplicates_sources_and_sets_exact_payloads` |
| `test_scope_v2.py::test_plain_x_is_not_a_twitter_alias` | 保留并适配 | `test_scope_v2.py::test_plain_x_is_not_a_twitter_alias` |
| `test_scope_v2.py::test_module_name_is_required` | 保留并适配 | `test_scope_v2.py::test_module_name_is_required` |
| `test_scope_v2.py::test_single_time_expression_bounds_search_at_requested_day` | 保留并适配 | `test_scope_v2.py::test_single_time_expression_bounds_search_at_requested_day` |
| `test_scope_v2.py::test_previous_month_clamps_end_of_month` | 保留并适配 | `test_scope_v2.py::test_previous_month_clamps_end_of_month` |
| `test_scope_v2.py::test_invalid_time_expressions` | 保留并适配 | `test_scope_v2.py::test_invalid_time_expressions` |
| `test_scope_v2.py::test_client_authenticates_and_retries_transient_server_error` | 保留并适配 | `test_scope_v2.py::test_client_authenticates_and_retries_transient_server_error` |
| `test_scope_v2.py::test_client_honors_retry_after_for_429` | 保留并适配 | `test_scope_v2.py::test_client_honors_retry_after_for_429` |
| `test_scope_v2.py::test_client_rejects_business_error` | 保留并适配 | `test_scope_v2.py::test_client_rejects_business_error` |
| `test_scope_v2.py::test_ecosystem_pages_backward_and_keeps_latest_day_not_after_target` | 保留并适配 | `test_scope_v2.py::test_ecosystem_pages_backward_and_keeps_latest_day_not_after_target` |
| `test_scope_v2.py::test_news_uses_one_server_side_month_range_request` | 保留并适配 | `test_scope_v2.py::test_news_uses_one_server_side_month_range_request` |
| `test_scope_v2.py::test_empty_date_bounded_search_stops_with_no_items` | 保留并适配 | `test_scope_v2.py::test_empty_date_bounded_search_stops_with_no_items` |
| `test_scope_v2.py::test_prefetch_returns_no_source_data_when_every_probe_is_empty` | 保留并适配 | `test_scope_v2.py::test_prefetch_returns_no_source_data_when_every_probe_is_empty` |
| `test_scope_v2.py::test_executor_persists_sanitized_failure` | 保留并适配 | `test_scope_v2.py::test_executor_persists_sanitized_failure` |
| `test_scope_v2.py::test_load_env_file_does_not_override_process_environment` | 保留并适配 | `test_scope_v2.py::test_load_env_file_does_not_override_process_environment` |
| `test_scope_v2.py::test_materialize_filtered_envelope_for_plain_pi` | 保留并适配 | `test_scope_v2.py::test_materialize_filtered_envelope_for_plain_pi` |
| `test_scope_v2.py::test_materialize_rejects_empty_prefetch` | 保留并适配 | `test_scope_v2.py::test_materialize_rejects_empty_prefetch` |
| `test_scope_v2_prompt.py::test_plain_pi_scope_v2_prompt_forbids_additional_acquisition` | 适配 | `test_workflow.py; test_pi_integration.py` |
| `test_scope_v2_prompt.py::test_pi_ontology_scope_v2_prompt_explains_hybrid_sources` | 适配 | `test_workflow.py; test_pi_integration.py` |
| `test_scope_v2_prompt.py::test_legacy_scope_has_no_scope_v2_prompt` | 适配 | `test_workflow.py; test_pi_integration.py` |
| `test_scope_v2_prompt.py::test_job_specific_contract_is_injected_into_prompt` | 适配 | `test_workflow.py; test_pi_integration.py` |
| `test_scope_v2_templates.py::test_intent_sources_automatically_select_template_fragments` | 保留并适配 | `test_scope_v2_templates.py::test_intent_sources_automatically_select_template_fragments` |
| `test_scope_v2_templates.py::test_every_source_fragment_can_be_composed_together` | 保留并适配 | `test_scope_v2_templates.py::test_every_source_fragment_can_be_composed_together` |
| `test_scope_v2_templates.py::test_scope_v2_template_runner_materializes_valid_rdf` | 保留并适配 | `test_scope_v2_templates.py::test_scope_v2_template_runner_materializes_valid_rdf` |
| `test_serve_host_script.py::test_host_script_does_not_reference_project_venv` | 适配 | `test_settings_and_distribution.py; deployment/README.md` |
| `test_serve_script.py::test_serve_script_automatically_loads_dotenv` | 适配 | `test_settings_and_distribution.py; deployment/README.md` |

具体适配说明见 [完整 JSON 清单](compatibility_matrix.json)。旧数据库兼容升级测试仅验证 store 的健壮性，不表示本次交付会接管旧实例历史数据。

在线建模的模板 `generatedBy` 与 generation_manifest.model 是原模板的离线生成 provenance，保留作为来源记录；它们不是当前在线模型配置。
