"""Agent chat, resume, runtime comparison, vector index and RAG evaluation endpoints."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from ..context import (
    ArchivedConversationError,
    ConversationNotFoundError,
    agent_clarification_result,
    prepare_turn,
    record_agent_turn,
    run_context_identity,
)
from ..db import connect, now, transaction
from ..security import actor_name
from .common import model_operation_error, require_case

router = APIRouter()


class AgentChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=3000)
    user_name: str = "本机律师"
    conversation_id: int | None = None
    mode: Literal["multi_agent", "langgraph"] = "multi_agent"
    use_llm: bool = True
    use_remote_embeddings: bool = True


@router.post("/api/cases/{case_id}/agent-chat")
def agent_chat(case_id: int, payload: AgentChatRequest):
    """Run the shared law-review agents on native DAG or LangGraph."""
    require_case(case_id)
    user_name = actor_name(payload.user_name)
    try:
        context = prepare_turn(
            case_id, payload.question, payload.conversation_id,
            user_name=user_name, use_llm=payload.use_llm,
        )
        runtime = "langgraph" if payload.mode == "langgraph" else "native"
        if context.resolution.needs_clarification:
            result = agent_clarification_result(context, runtime=runtime)
            result["conversation_id"] = record_agent_turn(
                case_id, context.identity(), result, source_run_id=None,
                audit_detail=f"{user_name}：{payload.question[:60]}",
            )
            return {**result, "mode": payload.mode, "user_name": user_name}
        if payload.mode == "langgraph":
            from ..langgraph_agents import create_langgraph_coordinator

            coordinator = create_langgraph_coordinator(case_id, payload.use_remote_embeddings)
        else:
            from ..agents import create_coordinator

            coordinator = create_coordinator(case_id, payload.use_remote_embeddings)
        result = coordinator.process_query(payload.question, user_name, payload.use_llm, context=context)
        # The run identity makes this write idempotent for retries/resumes.
        result["conversation_id"] = record_agent_turn(
            case_id, context.identity(), result, source_run_id=result.get("run_id"),
            audit_detail=f"{user_name}：{payload.question[:60]}",
        )
        return {
            **result,
            "mode": payload.mode,
            "user_name": user_name,
        }
    except ArchivedConversationError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ConversationNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ImportError as exc:
        raise HTTPException(503, "Agent 运行依赖不可用；请按锁文件重新安装依赖") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        if hasattr(exc, "run_id"):
            raise HTTPException(
                500,
                {
                    "message": "Agent 执行失败，请查看节点轨迹",
                    "run_id": exc.run_id,
                    "resumable": bool(getattr(exc, "resumable", False)),
                    "runtime": getattr(exc, "runtime", "langgraph"),
                    "checkpoint_thread_id": getattr(exc, "checkpoint_thread_id", ""),
                    "resume_count": getattr(exc, "resume_count", 0),
                },
            ) from exc
        raise HTTPException(500, "Agent 执行失败，请查看服务端诊断事件") from exc


@router.post("/api/agent-runs/{run_id}/resume")
def resume_agent_run(run_id: int):
    try:
        from ..langgraph_agents import resume_langgraph_run

        result = resume_langgraph_run(run_id)
        conn = connect()
        try:
            row = conn.execute("SELECT case_id FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        finally:
            conn.close()
        if row is not None:
            identity = run_context_identity(run_id)
            conversation_id = record_agent_turn(
                row["case_id"], identity, result, source_run_id=run_id,
                audit_action="恢复 LangGraph 运行",
                audit_detail=f"{actor_name()}：Run #{run_id}",
            )
            if conversation_id is not None:
                result["conversation_id"] = conversation_id
        return result
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        if hasattr(exc, "run_id"):
            raise HTTPException(
                500,
                {
                    "message": "恢复失败，请查看节点轨迹",
                    "run_id": exc.run_id,
                    "resumable": bool(getattr(exc, "resumable", False)),
                    "runtime": getattr(exc, "runtime", "langgraph"),
                    "checkpoint_thread_id": getattr(exc, "checkpoint_thread_id", ""),
                    "resume_count": getattr(exc, "resume_count", 0),
                },
            ) from exc
        raise HTTPException(500, "恢复失败，请查看服务端诊断事件") from exc


@router.post("/api/cases/{case_id}/agent-compare")
def compare_agent_runtimes(case_id: int, payload: AgentChatRequest):
    require_case(case_id)
    user_name = actor_name(payload.user_name)
    try:
        from ..agents import create_coordinator
        from ..langgraph_agents import create_langgraph_coordinator
        from ..rag import recall_memories
        from ..runtime_comparison import compare_runtime_results

        memory_snapshot = recall_memories(case_id, payload.question, 3, payload.use_remote_embeddings)
        native = create_coordinator(case_id, payload.use_remote_embeddings).process_query(
            payload.question, user_name, payload.use_llm,
            persist_memory=False, memory_snapshot=memory_snapshot,
        )
        langgraph = create_langgraph_coordinator(
            case_id, payload.use_remote_embeddings
        ).process_query(
            payload.question, user_name, payload.use_llm,
            persist_memory=False, memory_snapshot=memory_snapshot,
        )
        comparison = compare_runtime_results(native, langgraph)
        with transaction() as conn:
            conn.execute(
                "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, 'Agent 双运行时对比', ?, ?)",
                (case_id, f"{user_name}：{payload.question[:60]}", now()),
            )
        return {
            "question": payload.question,
            "native": native,
            "langgraph": langgraph,
            "comparison": comparison,
        }
    except Exception as exc:
        if hasattr(exc, "run_id"):
            raise HTTPException(
                500,
                {
                    "message": "双运行时对比失败，请查看节点轨迹",
                    "run_id": exc.run_id,
                    "resumable": bool(getattr(exc, "resumable", False)),
                    "runtime": getattr(exc, "runtime", "langgraph"),
                    "checkpoint_thread_id": getattr(exc, "checkpoint_thread_id", ""),
                    "resume_count": getattr(exc, "resume_count", 0),
                },
            ) from exc
        raise model_operation_error(exc, "双运行时对比失败，请查看服务端诊断事件") from exc


@router.post("/api/cases/{case_id}/vector-index")
def build_vector_index(case_id: int):
    """
    为案件构建向量索引

    展示向量检索能力（Agent 核心组件）
    """
    require_case(case_id)
    try:
        from ..rag import HybridRetriever

        tool = HybridRetriever(case_id, prefer_remote_embeddings=True)
        index = tool.ensure_vector_index(force=False)
        test_results, metrics = tool.retrieve("资金流向", limit=3)

        with transaction() as conn:
            conn.execute(
                "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '构建向量索引', ?, ?)",
                (case_id, f"已索引，测试检索返回 {len(test_results)} 条结果", now()),
            )

        return {
            "status": "success",
            "indexed": index,
            "test_results_count": len(test_results),
            "retrieval_metrics": metrics,
        }
    except ImportError as exc:
        raise HTTPException(503, "向量检索依赖不可用；请按锁文件重新安装依赖") from exc
    except Exception as exc:
        raise model_operation_error(exc, "索引失败，请查看服务端诊断事件") from exc


@router.get("/api/agent-runs/{run_id}")
def agent_run_trace(run_id: int):
    from ..agents import get_run_trace

    trace = get_run_trace(run_id)
    if not trace:
        raise HTTPException(404, "Agent 运行记录不存在")
    return trace


@router.post("/api/cases/{case_id}/evaluate-rag")
def evaluate_rag(case_id: int, ground_truth: list[dict[str, Any]] | None = Body(default=None)):
    require_case(case_id)
    from ..evaluation import evaluate_case

    try:
        return evaluate_case(case_id, prefer_remote_embeddings=True, k=5, ground_truth=ground_truth)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise model_operation_error(exc, "评测失败，请查看服务端诊断事件") from exc

