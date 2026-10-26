// Generated from FastAPI OpenAPI. Run pnpm contract:generate.
export interface paths {
    "/api/auth/session": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Login */
        post: operations["login_api_auth_session_post"];
        /** Logout */
        delete: operations["logout_api_auth_session_delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/me": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Identity */
        get: operations["identity_api_auth_me_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/oidc/login": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Oidc Login */
        get: operations["oidc_login_api_auth_oidc_login_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/oidc/callback": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Oidc Callback */
        get: operations["oidc_callback_api_auth_oidc_callback_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/agent-jobs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Submit Review */
        post: operations["submit_review_api_cases__case_id__agent_jobs_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agent-jobs/{job_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Review Status */
        get: operations["review_status_api_agent_jobs__job_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/members": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Members */
        get: operations["list_members_api_members_get"];
        put?: never;
        /** Create Member */
        post: operations["create_member_api_members_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/members/{member_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        /** Deactivate Member */
        delete: operations["deactivate_member_api_members__member_id__delete"];
        options?: never;
        head?: never;
        /** Update Member */
        patch: operations["update_member_api_members__member_id__patch"];
        trace?: never;
    };
    "/api/members/{member_id}/tokens": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Rotate Member Token */
        post: operations["rotate_member_token_api_members__member_id__tokens_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/members": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Case Members */
        get: operations["case_members_api_cases__case_id__members_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/members/{member_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Assign Case Member */
        put: operations["assign_case_member_api_cases__case_id__members__member_id__put"];
        post?: never;
        /** Remove Case Member */
        delete: operations["remove_case_member_api_cases__case_id__members__member_id__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/tasks": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Case Tasks */
        get: operations["list_case_tasks_api_cases__case_id__tasks_get"];
        put?: never;
        /** Create Task */
        post: operations["create_task_api_cases__case_id__tasks_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/tasks/mine": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** My Tasks */
        get: operations["my_tasks_api_tasks_mine_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/tasks/{task_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        /** Act On Task */
        patch: operations["act_on_task_api_tasks__task_id__patch"];
        trace?: never;
    };
    "/api/cases/{case_id}/reports": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Reports */
        get: operations["list_reports_api_cases__case_id__reports_get"];
        put?: never;
        /** Create Report */
        post: operations["create_report_api_cases__case_id__reports_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/reports/{report_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        /** Act On Report */
        patch: operations["act_on_report_api_reports__report_id__patch"];
        trace?: never;
    };
    "/api/health": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Health */
        get: operations["health_api_health_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/ready": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Ready
         * @description Readiness probe: legal verification is unavailable unless the corpus loaded intact.
         *
         *     Reports only availability, never the corpus path, so it can stay unauthenticated.
         */
        get: operations["ready_api_ready_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/model-config": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Read Model Config
         * @description Effective model endpoint and chat model, so the UI can prefill its form.
         */
        get: operations["read_model_config_api_model_config_get"];
        /**
         * Update Model Config
         * @description Persist an operator override for the local model endpoint and chat model.
         *
         *     The endpoint must satisfy the same egress policy the model call sites enforce,
         *     so the UI cannot point case text at an unapproved destination.
         */
        put: operations["update_model_config_api_model_config_put"];
        post?: never;
        /**
         * Reset Model Config
         * @description Drop the in-app override and fall back to the deployment environment.
         */
        delete: operations["reset_model_config_api_model_config_delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/model-config/test": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Test Model Config
         * @description Check an OpenAI-compatible local model service without exposing its response body.
         */
        post: operations["test_model_config_api_model_config_test_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/benchmarks/lawbench": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Lawbench Dataset */
        get: operations["lawbench_dataset_api_benchmarks_lawbench_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/dashboard": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Dashboard */
        get: operations["dashboard_api_dashboard_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Cases */
        get: operations["list_cases_api_cases_get"];
        put?: never;
        /** Create Case */
        post: operations["create_case_api_cases_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Case Detail */
        get: operations["case_detail_api_cases__case_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        /**
         * Update Case
         * @description 修改案件基本信息（带版本校验与字段级审计）。
         *
         *     建案后案号、当事人、类型和说明都可能需要更正；这里保留同一案件 ID，
         *     不通过重建案件来"修正"信息。带上 ``expected_version`` 可避免覆盖同事
         *     刚保存的修改。
         */
        patch: operations["update_case_api_cases__case_id__patch"];
        trace?: never;
    };
    "/api/cases/{case_id}/archive": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Archive Case */
        post: operations["archive_case_api_cases__case_id__archive_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/restore": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Restore Case */
        post: operations["restore_case_api_cases__case_id__restore_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/trash": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Trash Case */
        post: operations["trash_case_api_cases__case_id__trash_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/lifecycle/{lifecycle_status}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Lifecycle Cases */
        get: operations["lifecycle_cases_api_cases_lifecycle__lifecycle_status__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/documents": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Documents */
        get: operations["list_documents_api_cases__case_id__documents_get"];
        put?: never;
        /** Upload Documents */
        post: operations["upload_documents_api_cases__case_id__documents_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/batch-import": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Batch Import Documents
         * @description Stage bounded files, persist their manifest, then enqueue for recovery.
         */
        post: operations["batch_import_documents_api_cases__case_id__batch_import_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/batch-imports/{batch_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Batch Import Status
         * @description 查询批量导入任务状态
         */
        get: operations["get_batch_import_status_api_batch_imports__batch_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/documents/{document_id}/directory": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        /**
         * Update Directory
         * @description 人工校准材料目录，并记录改动前后值。
         */
        patch: operations["update_directory_api_documents__document_id__directory_patch"];
        trace?: never;
    };
    "/api/documents/{document_id}/pages/{page_no}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Document Page */
        get: operations["document_page_api_documents__document_id__pages__page_no__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        /**
         * Correct Page Text
         * @description 纠正单页原文，并让依赖该页的已确认证据退回待复核。
         *
         *     OCR 错字会直接污染检索与证据引用，因此允许人工纠错。纠错与审批失效在
         *     同一事务内完成：引用仍能在新原文中找到的证据保留确认，找不到的（含没有
         *     引用可核对的）退回待复核并清除确认人。
         */
        patch: operations["correct_page_text_api_documents__document_id__pages__page_no__patch"];
        trace?: never;
    };
    "/api/documents/{document_id}/file": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Document File */
        get: operations["document_file_api_documents__document_id__file_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/search": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Search */
        get: operations["search_api_cases__case_id__search_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/evidence": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Evidence */
        get: operations["list_evidence_api_cases__case_id__evidence_get"];
        put?: never;
        /**
         * Create Evidence
         * @description 手动创建证据事项
         */
        post: operations["create_evidence_api_cases__case_id__evidence_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/evidence/{evidence_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        /**
         * Delete Evidence
         * @description 删除证据事项（CASCADE 删除关联的标注和关系）
         */
        delete: operations["delete_evidence_api_evidence__evidence_id__delete"];
        options?: never;
        head?: never;
        /**
         * Update Evidence
         * @description 更新证据事项
         *
         *     协作约定：客户端带上读取时的 ``expected_version``；版本不一致返回 409，
         *     不会覆盖同事刚保存的内容。未携带时保留旧客户端行为。
         */
        patch: operations["update_evidence_api_evidence__evidence_id__patch"];
        trace?: never;
    };
    "/api/evidence/{evidence_id}/annotations": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Evidence Annotations */
        get: operations["list_evidence_annotations_api_evidence__evidence_id__annotations_get"];
        put?: never;
        /** Create Evidence Annotation */
        post: operations["create_evidence_annotation_api_evidence__evidence_id__annotations_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/evidence-annotations/{annotation_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        /** Delete Evidence Annotation */
        delete: operations["delete_evidence_annotation_api_evidence_annotations__annotation_id__delete"];
        options?: never;
        head?: never;
        /** Update Evidence Annotation */
        patch: operations["update_evidence_annotation_api_evidence_annotations__annotation_id__patch"];
        trace?: never;
    };
    "/api/cases/{case_id}/gap-analysis": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Gap Analysis
         * @description 获取案件的证据疏漏检测结果
         *
         *     参数:
         *         severity: 可选，按严重程度筛选 (高/中/低)
         */
        get: operations["get_gap_analysis_api_cases__case_id__gap_analysis_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/gap-analysis/preview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Preview Case Gap Analysis */
        get: operations["preview_case_gap_analysis_api_cases__case_id__gap_analysis_preview_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/gap-analysis/detect": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Detect And Save Case Gaps */
        post: operations["detect_and_save_case_gaps_api_cases__case_id__gap_analysis_detect_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/agent-tools/call": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Call Readonly Agent Tool
         * @description Call one bounded view-only tool; returned case content is untrusted data.
         */
        post: operations["call_readonly_agent_tool_api_cases__case_id__agent_tools_call_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/analyze": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Analyze */
        post: operations["analyze_api_cases__case_id__analyze_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/conversations": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Conversations */
        get: operations["list_conversations_api_cases__case_id__conversations_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/conversations/{conversation_id}/archive": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Archive Conversation */
        post: operations["archive_conversation_api_conversations__conversation_id__archive_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/conversations/{conversation_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        /** Delete Conversation */
        delete: operations["delete_conversation_api_conversations__conversation_id__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/conversations/{conversation_id}/messages": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Conversation Messages */
        get: operations["conversation_messages_api_conversations__conversation_id__messages_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/chat": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Case Chat */
        post: operations["case_chat_api_cases__case_id__chat_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/chat/stream": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Case Chat Stream
         * @description Token stream for one turn; the answer contract still validates in full.
         *
         *     Event contract (A01.1):
         *
         *     ``status``    ``phase`` = retrieval / generating / validating / replayed
         *     ``token``     provisional draft text (``provisional: true``); never a fact
         *     ``citations`` structured citations, emitted only after the contract passed
         *     ``final``     the complete answer payload; kept for older desktop builds
         *     ``retract``   failure terminal: discard the draft, nothing was committed
         *     ``done``      always last; ``status`` ok|failed, ids, ``ttft_ms``, ``usage``
         *
         *     Tokens are provisional: they reach the browser before ``validate_review_answer``
         *     has run. Streaming must never publish an answer that the non-streaming path
         *     would have rejected. A disconnect stops further business writes (the worker
         *     checks a cancel flag at the commit boundary); if the commit already happened,
         *     a retry with the same ``request_id`` replays the stored turn instead of
         *     appending a second one.
         */
        post: operations["case_chat_stream_api_cases__case_id__chat_stream_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/agent-chat": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Agent Chat
         * @description Run the shared law-review agents on native DAG or LangGraph.
         */
        post: operations["agent_chat_api_cases__case_id__agent_chat_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agent-runs/{run_id}/resume": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Resume Agent Run */
        post: operations["resume_agent_run_api_agent_runs__run_id__resume_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/agent-compare": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Compare Agent Runtimes */
        post: operations["compare_agent_runtimes_api_cases__case_id__agent_compare_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/vector-index": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Build Vector Index
         * @description 为案件构建向量索引
         *
         *     展示向量检索能力（Agent 核心组件）
         */
        post: operations["build_vector_index_api_cases__case_id__vector_index_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agent-runs/{run_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Agent Run Trace */
        get: operations["agent_run_trace_api_agent_runs__run_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/evaluate-rag": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Evaluate Rag */
        post: operations["evaluate_rag_api_cases__case_id__evaluate_rag_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/platform-metrics": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Case Platform Metrics */
        get: operations["case_platform_metrics_api_cases__case_id__platform_metrics_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/bank-transactions/import": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Import Bank Transactions */
        post: operations["import_bank_transactions_api_cases__case_id__bank_transactions_import_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/bank-transactions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Bank Transactions */
        get: operations["get_bank_transactions_api_cases__case_id__bank_transactions_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/bank-transactions/summary": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Bank Transaction Summary */
        get: operations["get_bank_transaction_summary_api_cases__case_id__bank_transactions_summary_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/bank-transactions/graph": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Bank Transaction Graph */
        get: operations["get_bank_transaction_graph_api_cases__case_id__bank_transactions_graph_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/export": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Export Case */
        get: operations["export_case_api_cases__case_id__export_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/export-templates": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Export Templates */
        get: operations["list_export_templates_api_export_templates_get"];
        put?: never;
        /** Create Export Template */
        post: operations["create_export_template_api_export_templates_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/export-templates/{template_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Export Template Detail */
        get: operations["get_export_template_detail_api_export_templates__template_id__get"];
        /** Update Export Template */
        put: operations["update_export_template_api_export_templates__template_id__put"];
        post?: never;
        /** Delete Export Template */
        delete: operations["delete_export_template_api_export_templates__template_id__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cases/{case_id}/audit": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Audit */
        get: operations["audit_api_cases__case_id__audit_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/system/health": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * System Health
         * @description Readiness for core storage; Redis is an optional, freshly probed dependency.
         *
         *     This is not a model/OCR/worker availability or writable-disk guarantee.
         */
        get: operations["system_health_api_system_health_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /** AgentChatRequest */
        AgentChatRequest: {
            /** Question */
            question: string;
            /**
             * User Name
             * @default 本机律师
             */
            user_name: string;
            /** Conversation Id */
            conversation_id?: number | null;
            /**
             * Mode
             * @default multi_agent
             * @enum {string}
             */
            mode: "multi_agent" | "langgraph";
            /**
             * Use Llm
             * @default true
             */
            use_llm: boolean;
            /**
             * Use Remote Embeddings
             * @default true
             */
            use_remote_embeddings: boolean;
        };
        /** AnnotationCreate */
        AnnotationCreate: {
            /**
             * User Name
             * @default 本机律师
             */
            user_name: string;
            /**
             * Annotation Type
             * @default 备注
             * @enum {string}
             */
            annotation_type: "备注" | "疑点" | "质证意见" | "补证建议" | "重点" | "其他";
            /** Content */
            content: string;
            /** Quote Start */
            quote_start?: number | null;
            /** Quote End */
            quote_end?: number | null;
            /**
             * Status
             * @default 待处理
             * @enum {string}
             */
            status: "待处理" | "已处理";
        };
        /** AnnotationUpdate */
        AnnotationUpdate: {
            /** Annotation Type */
            annotation_type?: ("备注" | "疑点" | "质证意见" | "补证建议" | "重点" | "其他") | null;
            /** Content */
            content?: string | null;
            /** Quote Start */
            quote_start?: number | null;
            /** Quote End */
            quote_end?: number | null;
            /** Status */
            status?: ("待处理" | "已处理") | null;
        };
        /** Body_batch_import_documents_api_cases__case_id__batch_import_post */
        Body_batch_import_documents_api_cases__case_id__batch_import_post: {
            /** Files */
            files: string[];
        };
        /** Body_import_bank_transactions_api_cases__case_id__bank_transactions_import_post */
        Body_import_bank_transactions_api_cases__case_id__bank_transactions_import_post: {
            /** File */
            file: string;
        };
        /** Body_upload_documents_api_cases__case_id__documents_post */
        Body_upload_documents_api_cases__case_id__documents_post: {
            /** Files */
            files: string[];
        };
        /** CaseCreate */
        CaseCreate: {
            /** Title */
            title: string;
            /**
             * Case No
             * @default
             */
            case_no: string;
            /**
             * Case Type
             * @default 刑事
             */
            case_type: string;
            /**
             * Client Name
             * @default
             */
            client_name: string;
            /**
             * Description
             * @default
             */
            description: string;
        };
        /** CaseRoleIn */
        CaseRoleIn: {
            /**
             * Role
             * @enum {string}
             */
            role: "owner" | "reviewer" | "assistant" | "viewer";
        };
        /** CaseUpdate */
        CaseUpdate: {
            /** Title */
            title?: string | null;
            /** Case No */
            case_no?: string | null;
            /** Case Type */
            case_type?: string | null;
            /** Client Name */
            client_name?: string | null;
            /** Status */
            status?: string | null;
            /** Description */
            description?: string | null;
            /** Expected Version */
            expected_version?: number | null;
        };
        /** ChatRequest */
        ChatRequest: {
            /** Question */
            question: string;
            /**
             * User Name
             * @default 本机律师
             */
            user_name: string;
            /** Conversation Id */
            conversation_id?: number | null;
            /**
             * Use Llm
             * @default true
             */
            use_llm: boolean;
            /**
             * Use Remote Embeddings
             * @default true
             */
            use_remote_embeddings: boolean;
            /** Request Id */
            request_id?: string | null;
        };
        /** DirectoryUpdate */
        DirectoryUpdate: {
            /** Doc Type */
            doc_type?: string | null;
            /** People */
            people?: string | null;
            /** Date Range */
            date_range?: string | null;
            /** Summary */
            summary?: string | null;
            /** Status */
            status?: string | null;
            /** Expected Version */
            expected_version?: number | null;
        };
        /** EvidenceCreate */
        EvidenceCreate: {
            /**
             * Title
             * @description 证据标题
             */
            title: string;
            /**
             * Category
             * @description 证据类别
             */
            category: string;
            /**
             * Fact
             * @description 待证事实
             */
            fact: string;
            /**
             * Credibility
             * @description 可信度
             * @default 待核验
             */
            credibility: string;
            /**
             * Status
             * @description 审核状态
             * @default 待复核
             */
            status: string;
            /**
             * Source Document Id
             * @description 来源文档ID
             */
            source_document_id?: number | null;
            /**
             * Source Page Start
             * @description 起始页码
             * @default 1
             */
            source_page_start: number;
            /**
             * Source Page End
             * @description 结束页码
             * @default 1
             */
            source_page_end: number;
            /**
             * Quote
             * @description 原文引用
             * @default
             */
            quote: string;
        };
        /** EvidenceUpdate */
        EvidenceUpdate: {
            /** Title */
            title?: string | null;
            /** Category */
            category?: string | null;
            /** Fact */
            fact?: string | null;
            /** Credibility */
            credibility?: string | null;
            /** Status */
            status?: string | null;
            /** Source Document Id */
            source_document_id?: number | null;
            /** Source Page Start */
            source_page_start?: number | null;
            /** Source Page End */
            source_page_end?: number | null;
            /** Quote */
            quote?: string | null;
            /** Expected Version */
            expected_version?: number | null;
        };
        /** ExportTemplateBlockIn */
        ExportTemplateBlockIn: {
            /**
             * Type
             * @enum {string}
             */
            type: "case_summary" | "catalog_csv" | "evidence_table" | "qa_log" | "static_markdown" | "attachments";
            /** Title */
            title: string;
            /** Filename */
            filename?: string | null;
            /**
             * Content
             * @default
             */
            content: string;
            /** Evidence Status */
            evidence_status?: ("待复核" | "已确认" | "待质证" | "待补证") | null;
            /**
             * Required
             * @default false
             */
            required: boolean;
        };
        /** ExportTemplateIn */
        ExportTemplateIn: {
            /** Name */
            name: string;
            /**
             * Description
             * @default
             */
            description: string;
            /** Blocks */
            blocks: components["schemas"]["ExportTemplateBlockIn"][];
        };
        /** HTTPValidationError */
        HTTPValidationError: {
            /** Detail */
            detail?: components["schemas"]["ValidationError"][];
        };
        /** Login */
        Login: {
            /** Token */
            token: string;
        };
        /** MemberAssignmentIn */
        MemberAssignmentIn: {
            /** Case Id */
            case_id: number;
            /**
             * Role
             * @default assistant
             * @enum {string}
             */
            role: "owner" | "reviewer" | "assistant" | "viewer";
        };
        /** MemberCreate */
        MemberCreate: {
            /** Name */
            name: string;
            /**
             * Admin
             * @default false
             */
            admin: boolean;
            /** Permissions */
            permissions?: string[];
            /** Assignments */
            assignments?: components["schemas"]["MemberAssignmentIn"][];
        };
        /** MemberUpdate */
        MemberUpdate: {
            /** Name */
            name?: string | null;
            /** Admin */
            admin?: boolean | null;
            /** Permissions */
            permissions?: string[] | null;
            /** Active */
            active?: boolean | null;
        };
        /** ModelConfigTest */
        ModelConfigTest: {
            /** Base Url */
            base_url: string;
        };
        /** ModelConfigUpdate */
        ModelConfigUpdate: {
            /** Base Url */
            base_url: string;
            /** Model */
            model: string;
        };
        /** PageTextUpdate */
        PageTextUpdate: {
            /** Text */
            text: string;
            /** Expected Version */
            expected_version?: number | null;
            /**
             * Reason
             * @default
             */
            reason: string;
        };
        /** ReadonlyToolCall */
        ReadonlyToolCall: {
            /** Tool */
            tool: string;
            /** Params */
            params?: {
                [key: string]: unknown;
            };
        };
        /** ReportAction */
        ReportAction: {
            /**
             * Action
             * @enum {string}
             */
            action: "submit" | "return" | "approve" | "void";
            /**
             * Note
             * @default
             */
            note: string;
        };
        /** ReportCreate */
        ReportCreate: {
            /**
             * Title
             * @default
             */
            title: string;
            /** Template Id */
            template_id?: number | null;
        };
        /** ReviewJobRequest */
        ReviewJobRequest: {
            /** Question */
            question: string;
            /** Conversation Id */
            conversation_id?: number | null;
            /**
             * User Name
             * @default 本机律师
             */
            user_name: string;
            /**
             * Mode
             * @default multi_agent
             * @enum {string}
             */
            mode: "multi_agent" | "langgraph";
            /**
             * Use Llm
             * @default true
             */
            use_llm: boolean;
            /**
             * Use Remote Embeddings
             * @default true
             */
            use_remote_embeddings: boolean;
        };
        /** TaskAction */
        TaskAction: {
            /**
             * Action
             * @enum {string}
             */
            action: "start" | "submit" | "return" | "complete" | "cancel" | "assign" | "due";
            /**
             * Note
             * @default
             */
            note: string;
            /** Assignee Member Id */
            assignee_member_id?: number | null;
            /** Due At */
            due_at?: string | null;
        };
        /** TaskCreate */
        TaskCreate: {
            /** Title */
            title: string;
            /**
             * Kind
             * @default 阅卷
             */
            kind: string;
            /**
             * Detail
             * @default
             */
            detail: string;
            /** Assignee Member Id */
            assignee_member_id?: number | null;
            /** Due At */
            due_at?: string | null;
            /** Evidence Id */
            evidence_id?: number | null;
        };
        /** ValidationError */
        ValidationError: {
            /** Location */
            loc: (string | number)[];
            /** Message */
            msg: string;
            /** Error Type */
            type: string;
            /** Input */
            input?: unknown;
            /** Context */
            ctx?: Record<string, never>;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    login_api_auth_session_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["Login"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    logout_api_auth_session_delete: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    identity_api_auth_me_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    oidc_login_api_auth_oidc_login_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    oidc_callback_api_auth_oidc_callback_get: {
        parameters: {
            query?: {
                code?: string;
                state?: string;
                error?: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    submit_review_api_cases__case_id__agent_jobs_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ReviewJobRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    review_status_api_agent_jobs__job_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                job_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_members_api_members_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    create_member_api_members_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MemberCreate"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    deactivate_member_api_members__member_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                member_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    update_member_api_members__member_id__patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                member_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MemberUpdate"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    rotate_member_token_api_members__member_id__tokens_post: {
        parameters: {
            query?: {
                label?: string;
            };
            header?: never;
            path: {
                member_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    case_members_api_cases__case_id__members_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    assign_case_member_api_cases__case_id__members__member_id__put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
                member_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CaseRoleIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    remove_case_member_api_cases__case_id__members__member_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
                member_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_case_tasks_api_cases__case_id__tasks_get: {
        parameters: {
            query?: {
                status?: string | null;
                mine?: boolean;
            };
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_task_api_cases__case_id__tasks_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["TaskCreate"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    my_tasks_api_tasks_mine_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    act_on_task_api_tasks__task_id__patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["TaskAction"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_reports_api_cases__case_id__reports_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_report_api_cases__case_id__reports_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ReportCreate"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    act_on_report_api_reports__report_id__patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                report_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ReportAction"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    health_api_health_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    ready_api_ready_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    read_model_config_api_model_config_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    update_model_config_api_model_config_put: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ModelConfigUpdate"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    reset_model_config_api_model_config_delete: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    test_model_config_api_model_config_test_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ModelConfigTest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    lawbench_dataset_api_benchmarks_lawbench_get: {
        parameters: {
            query?: {
                task?: string | null;
                limit?: number;
                seed?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    dashboard_api_dashboard_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    list_cases_api_cases_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    create_case_api_cases_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CaseCreate"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    case_detail_api_cases__case_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    update_case_api_cases__case_id__patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CaseUpdate"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    archive_case_api_cases__case_id__archive_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    restore_case_api_cases__case_id__restore_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    trash_case_api_cases__case_id__trash_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    lifecycle_cases_api_cases_lifecycle__lifecycle_status__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                lifecycle_status: "archived" | "trash";
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_documents_api_cases__case_id__documents_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    upload_documents_api_cases__case_id__documents_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "multipart/form-data": components["schemas"]["Body_upload_documents_api_cases__case_id__documents_post"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    batch_import_documents_api_cases__case_id__batch_import_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "multipart/form-data": components["schemas"]["Body_batch_import_documents_api_cases__case_id__batch_import_post"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_batch_import_status_api_batch_imports__batch_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                batch_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    update_directory_api_documents__document_id__directory_patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                document_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DirectoryUpdate"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    document_page_api_documents__document_id__pages__page_no__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                document_id: number;
                page_no: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    correct_page_text_api_documents__document_id__pages__page_no__patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                document_id: number;
                page_no: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PageTextUpdate"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    document_file_api_documents__document_id__file_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                document_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    search_api_cases__case_id__search_get: {
        parameters: {
            query: {
                q: string;
            };
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_evidence_api_cases__case_id__evidence_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_evidence_api_cases__case_id__evidence_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["EvidenceCreate"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_evidence_api_evidence__evidence_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                evidence_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    update_evidence_api_evidence__evidence_id__patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                evidence_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["EvidenceUpdate"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_evidence_annotations_api_evidence__evidence_id__annotations_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                evidence_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_evidence_annotation_api_evidence__evidence_id__annotations_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                evidence_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AnnotationCreate"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_evidence_annotation_api_evidence_annotations__annotation_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                annotation_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    update_evidence_annotation_api_evidence_annotations__annotation_id__patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                annotation_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AnnotationUpdate"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_gap_analysis_api_cases__case_id__gap_analysis_get: {
        parameters: {
            query?: {
                severity?: string;
            };
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    preview_case_gap_analysis_api_cases__case_id__gap_analysis_preview_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    detect_and_save_case_gaps_api_cases__case_id__gap_analysis_detect_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    call_readonly_agent_tool_api_cases__case_id__agent_tools_call_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ReadonlyToolCall"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    analyze_api_cases__case_id__analyze_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_conversations_api_cases__case_id__conversations_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    archive_conversation_api_conversations__conversation_id__archive_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_conversation_api_conversations__conversation_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    conversation_messages_api_conversations__conversation_id__messages_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    case_chat_api_cases__case_id__chat_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ChatRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    case_chat_stream_api_cases__case_id__chat_stream_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ChatRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    agent_chat_api_cases__case_id__agent_chat_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AgentChatRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    resume_agent_run_api_agent_runs__run_id__resume_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    compare_agent_runtimes_api_cases__case_id__agent_compare_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AgentChatRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    build_vector_index_api_cases__case_id__vector_index_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    agent_run_trace_api_agent_runs__run_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    evaluate_rag_api_cases__case_id__evaluate_rag_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                }[] | null;
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    case_platform_metrics_api_cases__case_id__platform_metrics_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    import_bank_transactions_api_cases__case_id__bank_transactions_import_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "multipart/form-data": components["schemas"]["Body_import_bank_transactions_api_cases__case_id__bank_transactions_import_post"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_bank_transactions_api_cases__case_id__bank_transactions_get: {
        parameters: {
            query?: {
                limit?: number;
                offset?: number;
                account?: string | null;
                direction?: ("inflow" | "outflow" | "unknown") | null;
                counterparty?: string | null;
                date_from?: string | null;
                date_to?: string | null;
            };
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_bank_transaction_summary_api_cases__case_id__bank_transactions_summary_get: {
        parameters: {
            query?: {
                account?: string | null;
                direction?: ("inflow" | "outflow" | "unknown") | null;
                counterparty?: string | null;
                date_from?: string | null;
                date_to?: string | null;
            };
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_bank_transaction_graph_api_cases__case_id__bank_transactions_graph_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    export_case_api_cases__case_id__export_get: {
        parameters: {
            query?: {
                template_id?: number | null;
                final?: boolean;
            };
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_export_templates_api_export_templates_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    create_export_template_api_export_templates_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ExportTemplateIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_export_template_detail_api_export_templates__template_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                template_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    update_export_template_api_export_templates__template_id__put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                template_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ExportTemplateIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_export_template_api_export_templates__template_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                template_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    audit_api_cases__case_id__audit_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                case_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    system_health_api_system_health_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
}
