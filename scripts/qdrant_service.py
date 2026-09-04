#!/usr/bin/env python3
"""Memory OS Qdrant Service - Vector Database"""
import os, sys, json

class QdrantService:
    def __init__(self, host="127.0.0.1", port=6333):
        self.host = host
        self.port = port
        self.client = None
        try:
            from qdrant_client import QdrantClient
            self.client = QdrantClient(host=host, port=port)
            print("Qdrant connected")
        except ImportError:
            print("qdrant-client not installed: pip install qdrant-client")
        except Exception as e:
            print(f"Qdrant connection failed: {e}")

    def create_collection(self, name, vector_size=1024):
        if not self.client:
            return {"error": "Not connected"}
        from qdrant_client.models import Distance, VectorParams
        self.client.recreate_collection(
            collection_name=name,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE)
        )
        return {"status": "created", "collection": name}

    def upsert(self, collection_name, points):
        if not self.client:
            return {"error": "Not connected"}
        self.client.upsert(collection_name=collection_name, points=points)
        return {"status": "ok", "count": len(points)}

    def search(self, collection_name, query_vector, limit=5):
        if not self.client:
            return {"error": "Not connected"}
        results = self.client.search(
            collection_name=collection_name,
            query_vector=query_vector,
            limit=limit
        )
        return [{"id": r.id, "score": r.score, "payload": r.payload} for r in results]

if __name__ == "__main__":
    service = QdrantService()
    print(json.dumps({"status": "ok", "service": "Qdrant"}))
