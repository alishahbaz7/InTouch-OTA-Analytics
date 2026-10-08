"""The command library as a file: export, import, and what an import would change."""

from __future__ import annotations

from ota_analytics import cota, cota_library, db

GET_FTP, CLR_SOS, SET_6B82 = "DAD76F4B", "DDD76D66", "DBD76B82D531"


def _seed(conn):
    cota_library.save_parameter(conn, "6C0A", "TIMERS")
    cota_library.save_command(conn, name="Read FTP", val1=GET_FTP, tags="read")
    cota_library.save_command(conn, name="SOS off", val1=CLR_SOS, tags="alarm", note="clears it")
    cota_library.save_command(conn, name="Ignition 1 s", val1=SET_6B82)


def test_saved_commands_are_grouped_get_set_clr_then_by_name(conn):
    _seed(conn)
    cota_library.save_command(conn, name="A clear", val1="DDD76F87")
    assert [c["name"] for c in cota_library.commands(conn)] == [
        "Read FTP", "Ignition 1 s", "A clear", "SOS off"]


def test_a_parameter_says_how_many_saved_commands_use_it(conn):
    _seed(conn)
    cota_library.save_command(conn, name="Set timers", val1="DBD76C0AD531")
    cota_library.save_command(conn, name="Get timers", val1="DAD76C0A")
    used = {p["code"]: p["used_by"] for p in cota_library.parameters(conn)}
    assert used["6C0A"] == 2 and used["6D66"] == 1 and used["6F4B"] == 1
    assert [p["code"] for p in cota_library.parameters(conn, q="tim")] == ["6C0A"]


def test_the_same_command_under_another_name_is_found(conn):
    _seed(conn)
    other = cota_library.save_command(conn, name="Clear SOS", val1=CLR_SOS)
    assert cota_library.same_command(conn, CLR_SOS, other) == ["SOS off"]
    assert cota_library.same_command(conn, GET_FTP) == ["Read FTP"]


def test_an_export_loads_into_another_install_unchanged(conn, tmp_path):
    _seed(conn)
    exported = cota_library.export_csv(conn)
    assert exported.splitlines()[0] == "kind,name,command,tags,note"
    assert "parameter,TIMERS,6C0A,," in exported and "6F4B" not in exported.split("command,")[0]
    theirs = db.connect(tmp_path / "theirs.db")
    plan = cota_library.plan_import(theirs, exported.encode("utf-8"))
    assert plan["counts"] == {"new": 4, "updated": 0, "same": 0, "error": 0}
    cota_library.apply_import(theirs, exported.encode("utf-8"))
    assert cota_library.export_csv(theirs) == exported
    again = cota_library.plan_import(theirs, exported.encode("utf-8"))
    assert again["counts"] == {"new": 0, "updated": 0, "same": 4, "error": 0}


def test_an_import_says_what_it_would_change_and_writes_nothing(conn):
    _seed(conn)
    content = ("kind,name,command,tags,note\n"
               "parameter,TIMERS-2,6C0A,,\n"                    # renames a parameter
               "command,Read FTP,DAD76F4B,read,\n"                # the same
               "command,SOS off,DDD76D66,alarm,new note\n"        # changes the note
               "command,New one,DAD76F87,,\n"                     # new
               "command,Broken,hello,,\n"                         # not a command
               "thing,X,1,,\n"                                    # not a kind
               "\n").encode("utf-8")
    plan = cota_library.plan_import(conn, content)
    actions = [(r["line"], r["action"]) for r in plan["rows"]]
    assert actions == [(2, "update"), (3, "same"), (4, "update"), (5, "new"), (6, "error"), (7, "error")]
    assert plan["rows"][0]["detail"] == "was “TIMERS”" and plan["rows"][2]["detail"] == "changes note"
    assert "hexadecimal" in plan["rows"][4]["detail"] and "kind must be" in plan["rows"][5]["detail"]
    assert cota.describe_command("DAD76C0A") == "GET TIMERS"             # nothing written yet


def test_an_import_adds_and_updates_but_never_deletes(conn):
    _seed(conn)
    content = ("kind,name,command,tags,note\n"
               "command,SOS off,DDD76D66,alarm,new note\n"
               "command,New one,DAD76F87,,\n").encode("utf-8")
    counts = cota_library.apply_import(conn, content)
    assert counts == {"new": 1, "updated": 1, "same": 0, "error": 0}
    names = {c["name"]: c for c in cota_library.commands(conn)}
    assert set(names) == {"Read FTP", "SOS off", "Ignition 1 s", "New one"}   # nothing removed
    assert names["SOS off"]["note"] == "new note"


def test_a_file_that_is_not_a_library_is_refused_with_the_reason(conn):
    assert "kind, name and command" in cota_library.plan_import(conn, b"a,b,c\n1,2,3\n")["error"]
    assert "Not a text file" in cota_library.plan_import(conn, b"\xff\xfe\x00\x81")["error"]
    assert cota_library.plan_import(conn, b"kind,name,command\n")["error"] == "The file has no rows."


def test_the_template_imports_cleanly(conn):
    plan = cota_library.plan_import(conn, cota_library.TEMPLATE_CSV.encode("utf-8"))
    assert plan["counts"]["error"] == 0 and plan["counts"]["new"] == 3


def test_one_command_saved_under_two_names_is_named_by_the_first(conn):
    cota_library.save_command(conn, name="SOS off", val1=CLR_SOS)
    cota_library.save_command(conn, name="Clear SOS", val1=CLR_SOS)
    assert cota.describe_command(CLR_SOS) == "SOS off"
