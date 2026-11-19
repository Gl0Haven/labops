import sqlite3
import unittest

from labops.db import migrate, tx
from labops.errors import IllegalTransition
from labops.statemachine import TRANSITIONS, transition


def seeded_conn(entity: str, state: str):
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('r','/r','t')")
    conn.execute("INSERT INTO acceptance_profiles(profile_hash,content_json,created_at) VALUES('p','{}','t')")
    conn.execute("INSERT INTO tasks(task_id,repo_id,goal,state,base_commit,profile_hash,created_at,updated_at) VALUES('T20261019-x','r','g','ACTIVE','a','p','t','t')")
    if entity == "task":
        conn.execute("UPDATE tasks SET state=?", (state,))
        return conn, "T20261019-x"
    conn.execute("INSERT INTO rounds(round_id,task_id,n,actor,role,base_commit,state,created_at,updated_at) VALUES('T20261019-x/r1','T20261019-x',1,'codex','implement','a','OPEN','t','t')")
    if entity == "round":
        conn.execute("UPDATE rounds SET state=?", (state,))
        return conn, "T20261019-x/r1"
    if entity == "review":
        conn.execute("INSERT INTO reviews(review_id,task_id,round_id,reviewer,state,base_commit,reviewed_commit,reviewed_tree,scope_id,acceptance_profile_hash,created_at,updated_at) VALUES('rv','T20261019-x','T20261019-x/r1','opus',?,'a','b','c','none','p','t','t')", (state,))
        return conn, "rv"
    if entity == "integration":
        conn.execute("INSERT INTO integrations(integration_id,task_id,candidate_commit,candidate_tree,main_before,state,created_at,updated_at) VALUES('ig','T20261019-x','b','c','a',?,'t','t')", (state,))
        return conn, "ig"
    if entity == "operation":
        conn.execute("INSERT INTO operations(op_id,repo_id,kind,status,idempotency_id,created_at,updated_at) VALUES('op','r','create_ref',?,'idem','t','t')", (state,))
        return conn, "op"
    raise AssertionError(entity)


class StateMachineTests(unittest.TestCase):
    def test_every_state_pair_for_every_entity(self):
        for entity, transitions in TRANSITIONS.items():
            states = list(transitions)
            for source in states:
                for target in states:
                    conn, entity_id = seeded_conn(entity, source)
                    allowed = target in transitions[source]
                    if allowed:
                        with tx(conn):
                            transition(conn, entity, entity_id, target, actor="test", detail={"case": "table"})
                        table, key, statecol = {
                            "task": ("tasks", "task_id", "state"), "round": ("rounds", "round_id", "state"),
                            "review": ("reviews", "review_id", "state"), "integration": ("integrations", "integration_id", "state"),
                            "operation": ("operations", "op_id", "status"),
                        }[entity]
                        self.assertEqual(conn.execute(f"SELECT {statecol} FROM {table} WHERE {key}=?", (entity_id,)).fetchone()[0], target)
                        event = conn.execute("SELECT from_state,to_state,actor,detail_json FROM events").fetchone()
                        self.assertEqual(tuple(event[:3]), (source, target, "test"))
                        self.assertEqual(event[3], '{"case":"table"}')
                    else:
                        with self.assertRaises(IllegalTransition):
                            with tx(conn):
                                transition(conn, entity, entity_id, target, actor="test")
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
                    conn.close()


if __name__ == "__main__":
    unittest.main()
