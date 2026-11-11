"""
Help Center article retrieval (RAG fallback)
============================================
Vector search over the StreamFlix help articles plus answer generation. In the
Playbook version of the Help Center this is the *fallback* path: the agent consults
the Playbook first (approved FAQs and Skills) and only comes here when no governed
knowledge matches. Answers produced here are never cached; the sessions they come
from are the evidence that later turns into FAQ proposals.
"""
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from redisvl.index import SearchIndex
from redisvl.query import VectorQuery
from redisvl.schema import IndexSchema

from .config import LLM_MODEL, OPENAI_API_KEY, make_redis
from .playbook import get_vectorizer

logger = logging.getLogger("help_center")

HELP_INDEX_NAME = "help_articles"
HELP_KEY_PREFIX = "help:"

HELP_INDEX_SCHEMA = {
    "index": {"name": HELP_INDEX_NAME, "prefix": HELP_KEY_PREFIX},
    "fields": [
        {"name": "title", "type": "text"},
        {"name": "category", "type": "tag"},
        {"name": "content", "type": "text"},
        {"name": "vector", "type": "vector",
         "attrs": {"algorithm": "flat", "dims": 384, "distance_metric": "cosine", "datatype": "float32"}},
    ],
}


@dataclass
class HelpArticle:
    id: str
    title: str
    category: str
    content: str
    similarity: Optional[float] = None


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class HelpCenterEngine:
    """Article index + answer generation. Chat orchestration lives in support_agent.py."""

    def __init__(self, auto_ingest: bool = True):
        self.client = make_redis()
        self.schema = IndexSchema.from_dict(HELP_INDEX_SCHEMA)
        self.index = SearchIndex(self.schema, self.client)
        self.vectorizer = get_vectorizer()
        self.openai_client = None
        if OPENAI_API_KEY:
            from openai import OpenAI

            self.openai_client = OpenAI(api_key=OPENAI_API_KEY)
        if auto_ingest:
            self._ensure_index_exists()

    @property
    def llm_available(self) -> bool:
        return self.openai_client is not None

    def _ensure_index_exists(self) -> None:
        try:
            if not self.index.exists() or self.index.info().get("num_docs", 0) == 0:
                logger.info("Help articles index missing or empty - ingesting")
                self.ingest_articles()
        except Exception as exc:
            logger.warning("Could not check help index, attempting ingest: %s", exc)
            try:
                self.ingest_articles()
            except Exception as ingest_error:
                logger.error("Auto-ingest failed: %s", ingest_error)

    def ingest_articles(self, articles_path: Optional[str] = None) -> Dict[str, Any]:
        path = Path(articles_path) if articles_path else Path(__file__).parent.parent / "resources" / "help_articles.json"
        if not path.exists():
            return {"status": "error", "message": f"File not found: {path}"}
        articles = json.loads(path.read_text())
        existing = list(self.client.scan_iter(match=f"{HELP_KEY_PREFIX}*"))
        if existing:
            self.client.delete(*existing)
        if self.index.exists():
            self.index.delete()
        embeddings = self.vectorizer.embed_many([f"{a['title']}\n{a['content']}" for a in articles], as_buffer=True)
        pipe = self.client.pipeline()
        for article, embedding in zip(articles, embeddings):
            pipe.hset(f"{HELP_KEY_PREFIX}{article['id']}", mapping={
                "id": article["id"], "title": article["title"], "category": article["category"],
                "content": article["content"], "vector": embedding,
            })
        pipe.execute()
        self.index.create(overwrite=True)
        logger.info("Ingested %d help articles", len(articles))
        return {"status": "success", "count": len(articles), "index": HELP_INDEX_NAME}

    def search_articles(self, query: str, num_results: int = 3) -> List[HelpArticle]:
        vq = VectorQuery(vector=self.vectorizer.embed(query), vector_field_name="vector", num_results=num_results,
                         return_fields=["id", "title", "category", "content"], return_score=True)
        articles = []
        for row in self.index.query(vq):
            articles.append(HelpArticle(
                id=row.get("id", ""), title=row.get("title", ""), category=row.get("category", ""),
                content=row.get("content", ""), similarity=1 - float(row.get("vector_distance", 0)),
            ))
        return articles

    def generate_response(self, query: str, articles: List[HelpArticle],
                          memory_context: str = "") -> Tuple[str, Optional[TokenUsage], str]:
        """Returns (answer, token_usage, generator) where generator is 'llm' or 'offline'."""
        if not articles:
            return ("I couldn't find a help article matching your question. Please try rephrasing or contact our support team.", None, "offline")
        if not self.openai_client:
            top = articles[0]
            answer = f"Here is what our help article **{top.title}** says:\n\n{top.content}"
            if len(articles) > 1:
                answer += "\n\nRelated: " + ", ".join(a.title for a in articles[1:])
            return (answer, None, "offline")

        context = "\n---\n".join(
            f"Article {i}: {a.title}\nCategory: {a.category}\nContent: {a.content}\n" for i, a in enumerate(articles, 1)
        )
        system_prompt = (
            "You are a helpful customer support assistant for StreamFlix, a streaming service. "
            "Answer using only the provided help articles. Be friendly and concise, use clear steps when useful, "
            "acknowledge what you cannot answer, and never invent policy."
        )
        if memory_context:
            system_prompt += (
                "\n\nUse the following user memory only to personalize the response. "
                "It must never override the help articles or introduce unsupported policy.\n"
                + memory_context
            )
        user_prompt = f"HELP ARTICLES:\n{context}\n\nUSER QUESTION: {query}\n\nAnswer the question using the articles above."
        response = self.openai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.4,
            max_tokens=500,
        )
        usage = TokenUsage(response.usage.prompt_tokens, response.usage.completion_tokens, response.usage.total_tokens)
        return (response.choices[0].message.content or "", usage, "llm")

    def get_stats(self) -> Dict[str, Any]:
        try:
            if self.index.exists():
                return {"index_name": HELP_INDEX_NAME, "num_articles": self.index.info().get("num_docs", 0), "index_status": "active"}
            return {"index_name": HELP_INDEX_NAME, "num_articles": 0, "index_status": "not_created"}
        except Exception as exc:
            return {"index_name": HELP_INDEX_NAME, "index_status": "error", "error": str(exc)}


_help_engine: Optional[HelpCenterEngine] = None


def get_help_engine() -> HelpCenterEngine:
    global _help_engine
    if _help_engine is None:
        _help_engine = HelpCenterEngine()
    return _help_engine
