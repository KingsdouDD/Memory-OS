#!/usr/bin/env python3
"""Memory OS Neo4j Service"""
import os, sys, json

class Neo4jService:
    def __init__(self, uri="bolt://127.0.0.1:7687", user="neo4j", password="memoryos_local"):
        self.uri = uri
        self.user = user
        self.password = password
        self.driver = None
        try:
            from neo4j import GraphDatabase
            self.driver = GraphDatabase.driver(uri, auth=(user, password))
            print("Neo4j connected")
        except ImportError:
            print("neo4j package not installed: pip install neo4j")
        except Exception as e:
            print(f"Neo4j connection failed: {e}")

    def close(self):
        if self.driver:
            self.driver.close()

    def create_node(self, label, properties):
        if not self.driver:
            return {"error": "Not connected"}
        with self.driver.session() as session:
            query = f"CREATE (n:{label} $props) RETURN n"
            result = session.run(query, props=properties)
            return result.single()

    def create_relation(self, subject, predicate, obj, properties=None):
        if not self.driver:
            return {"error": "Not connected"}
        props = properties or {}
        props["status"] = props.get("status", "active")
        with self.driver.session() as session:
            query = """
            MERGE (a {name: $subject})
            MERGE (b {name: $object})
            MERGE (a)-[r {predicate: $predicate}]->(b)
            SET r += $props
            RETURN a, r, b
            """
            result = session.run(query, subject=subject, object=obj, predicate=predicate, props=props)
            return result.single()

    def query(self, cypher):
        if not self.driver:
            return {"error": "Not connected"}
        with self.driver.session() as session:
            result = session.run(cypher)
            return [dict(record) for record in result]

if __name__ == "__main__":
    service = Neo4jService()
    print(json.dumps({"status": "ok", "service": "Neo4j"}))
    service.close()
