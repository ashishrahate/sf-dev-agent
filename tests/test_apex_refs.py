"""Unit tests for the Apex class-reference extractor."""

from __future__ import annotations

from sf_dev_agent.context.parsers._apex_refs import extract_class_references


def test_extracts_static_method_call() -> None:
    src = "public class Caller { static { Foo.bar(); } }"
    refs = extract_class_references(src)
    assert "Foo" in refs


def test_extracts_constructor_invocation() -> None:
    src = "public class Caller { void m() { Bar b = new Bar(); } }"
    refs = extract_class_references(src)
    assert "Bar" in refs


def test_extracts_type_in_variable_declaration() -> None:
    src = "public class Caller { Helper h; }"
    refs = extract_class_references(src)
    assert "Helper" in refs


def test_filters_apex_builtins() -> None:
    src = """
    public class Caller {
        public void m() {
            System.debug('hi');
            Database.query('SELECT Id FROM Account');
            List<Account> accs = [SELECT Id FROM Account LIMIT 1];
            String s = JSON.serialize(accs);
            Test.startTest();
        }
    }
    """
    refs = extract_class_references(src)
    builtins = {"System", "Database", "List", "String", "JSON", "Test", "Account"}
    assert builtins.isdisjoint(refs), \
        f"Built-ins should be filtered, got: {refs & builtins}"


def test_ignores_string_literals() -> None:
    """Class names that appear inside string literals should not become refs."""
    src = "public class Caller { String x = 'see SneakyClass.staticMethod()'; }"
    refs = extract_class_references(src)
    assert "SneakyClass" not in refs


def test_ignores_comments() -> None:
    """Class names inside // and /* */ comments should not become refs."""
    src = """
    public class Caller {
        // CommentedClass.method();
        /* MultiLineRef m = new MultiLineRef(); */
        public void real() { ActualRef.go(); }
    }
    """
    refs = extract_class_references(src)
    assert "CommentedClass" not in refs
    assert "MultiLineRef" not in refs
    assert "ActualRef" in refs


def test_exclude_drops_self_and_supplied_names() -> None:
    src = "public class Caller extends Base { void m() { Caller.x(); Helper.y(); } }"
    refs = extract_class_references(src, exclude={"Caller", "Base"})
    assert "Caller" not in refs
    assert "Base" not in refs
    assert "Helper" in refs


def test_filters_all_caps_keywords() -> None:
    """SOQL keywords like ALL, FROM, NULL aren't real class references."""
    src = "public class Caller { Map<Id, Account> m = new Map<Id, Account>(); }"
    refs = extract_class_references(src)
    assert "ALL" not in refs
    assert "NULL" not in refs


def test_handles_empty_source() -> None:
    assert extract_class_references("") == set()
    assert extract_class_references("   \n\n  ") == set()
