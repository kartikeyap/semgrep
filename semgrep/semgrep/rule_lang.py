import hashlib
from io import StringIO
from typing import Any
from typing import Dict
from typing import Generic
from typing import ItemsView
from typing import KeysView
from typing import List
from typing import NewType
from typing import Optional
from typing import TypeVar
from typing import Union

import attr
from ruamel.yaml import Node
from ruamel.yaml import RoundTripConstructor
from ruamel.yaml import YAML

from semgrep.constants import PLEASE_FILE_ISSUE_TEXT

# Do not construct directly, use `SpanBuilder().add_source`
SourceFileHash = NewType("SourceFileHash", str)


class SourceTracker:
    """
    Singleton class tracking mapping from filehashes -> file contents to support
    building error messages from Spans
    """

    # sources are a class variable to share state
    sources: Dict[SourceFileHash, List[str]] = {}

    @classmethod
    def add_source(cls, source: str) -> SourceFileHash:
        file_hash = cls._src_to_hash(source)
        cls.sources[file_hash] = source.splitlines()
        return file_hash

    @classmethod
    def source(cls, source_hash: SourceFileHash) -> List[str]:
        return cls.sources[source_hash]

    @staticmethod
    def _src_to_hash(contents: Union[str, bytes]) -> SourceFileHash:
        if isinstance(contents, str):
            contents = contents.encode("utf-8")
        return SourceFileHash(hashlib.sha256(contents).hexdigest())


@attr.s(auto_attribs=True, frozen=True, repr=False)
class Position:
    """
    Position within a file.
    :param line: 1-indexed line number
    :param col: 1-indexed column number

    line & column are 0 indexed for compatibility with semgrep-core which also produces 1-indexed results
    """

    line: int
    col: int

    def next_line(self) -> "Position":
        return attr.evolve(self, line=self.line + 1)

    def previous_line(self) -> "Position":
        return attr.evolve(self, line=self.line - 1)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} line={self.line} col={self.col}>"


@attr.s(auto_attribs=True, frozen=True, repr=False)
class Span:
    """
    Spans are immutable objects, representing segments of code. They have a central focus area, and
    optionally can contain surrounding context.
    """

    start: Position
    end: Position
    source_hash: SourceFileHash
    file: Optional[str]
    context_start: Optional[Position] = None
    context_end: Optional[Position] = None

    @classmethod
    def from_node(
        cls, node: Node, source_hash: SourceFileHash, filename: Optional[str]
    ) -> "Span":
        start = Position(line=node.start_mark.line + 1, col=node.start_mark.column + 1)
        end = Position(line=node.end_mark.line + 1, col=node.end_mark.column + 1)
        return Span(start=start, end=end, file=filename, source_hash=source_hash)

    def truncate(self, lines: int) -> "Span":
        """
        Produce a new span truncated to at most `lines` starting from the start line.
        - start_context is not considered.
        - end_context is removed
        """
        if self.end.line - self.start.line > lines:
            return attr.evolve(
                self,
                end=Position(line=self.start.line + lines, col=0),
                context_end=None,
            )
        return self

    def extend_to(self, span: "Span", context_only: bool = True) -> "Span":
        """
        Extend this span to go as far as `span`.
        :param span: The span to extend to
        :param context_only: If true, the additional lines will only be marked as context. These will be displayed,
        but unlike to core span area, they won't be highlighted in error messages.
        """
        if context_only:
            return attr.evolve(self, context_end=span.context_end or span.end)
        else:
            return attr.evolve(self, end=span.end, context_end=span.context_end)

    def with_context(
        self, before: Optional[int] = None, after: Optional[int] = None
    ) -> "Span":
        """
        Expand
        """
        new = self
        if before is not None:
            new = attr.evolve(
                new,
                context_start=Position(col=0, line=max(0, self.start.line - before)),
            )

        if after is not None:
            new = attr.evolve(
                new,
                context_end=Position(
                    col=0,
                    line=min(
                        len(SourceTracker.source(self.source_hash)),
                        self.end.line + after,
                    ),
                ),
            )
        return new

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} start={self.start} end={self.end}>"


# Actually recursive but mypy is unhelpful
YamlValue = Union[str, int, List[Any], Dict[str, Any]]
LocatedYamlValue = Union[str, int, List["YamlTree"], "YamlMap"]

T = TypeVar("T", bound=LocatedYamlValue)


class YamlTree(Generic[T]):
    def __init__(self, value: T, span: Span):
        self.value = value
        self.span = span

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} span={self.span} value={self.value}>"

    def unroll_dict(self) -> Dict[str, Any]:
        """
        Helper wrapper mostly for mypy when you know it contains a dictionary
        """
        ret = self.unroll()
        if not isinstance(ret, dict):
            raise ValueError(
                f"unroll_dict called but object was actually {type(ret).__name__}"
            )
        return ret

    def unroll(self) -> YamlValue:
        """
        Recursively expand the `self.value`, converting back to a normal datastructure
        """
        if isinstance(self.value, list):
            return [x.unroll() for x in self.value]
        elif isinstance(self.value, YamlMap):
            return {str(k.unroll()): v.unroll() for k, v in self.value.items()}
        elif isinstance(self.value, YamlTree):
            return self.value.unroll()
        elif isinstance(self.value, str) or isinstance(self.value, int):
            return self.value
        else:
            raise ValueError("Invalid YAML tree structure")

    @classmethod
    def wrap(cls, value: YamlValue, span: Span) -> "YamlTree":  # type: ignore
        """
        Wraps a value in a YamlTree and attaches the span everywhere.
        This exists so you can take generate a datastructure from user input, but track all the errors within that
        datastructure back to the user input
        """
        if isinstance(value, list):
            return YamlTree(value=[YamlTree.wrap(x, span) for x in value], span=span)
        elif isinstance(value, dict):
            return YamlTree(
                value=YamlMap(
                    {
                        YamlTree.wrap(k, span): YamlTree.wrap(v, span)
                        for k, v in value.items()
                    }
                ),
                span=span,
            )
        elif isinstance(value, YamlTree):
            return value
        else:
            return YamlTree(value, span)


class YamlMap:
    """
    To preserve span information for keys, which we commonly use in error messages,
    make a custom map type that is indexable by str, but provides views into all
    necessary spans
    """

    def __init__(self, internal: Dict[YamlTree[str], YamlTree]):
        self._internal = internal

    def __getitem__(self, key: str) -> YamlTree:
        return next(v for k, v in self._internal.items() if k.value == key)

    def __setitem__(self, key: YamlTree[str], value: YamlTree) -> None:
        self._internal[key] = value

    def items(self) -> ItemsView[YamlTree[str], YamlTree]:
        return self._internal.items()

    def key_tree(self, key: str) -> YamlTree[str]:
        return next(k for k, v in self._internal.items() if k.value == key)

    def get(self, key: str) -> Optional[YamlTree]:
        match = [v for k, v in self._internal.items() if k.value == key]
        if match:
            return match[0]
        return None

    def keys(self) -> KeysView[YamlTree[str]]:
        return self._internal.keys()


def parse_yaml(contents: str) -> Dict[str, Any]:
    # this uses the `RoundTripConstructor` which inherits from `SafeConstructor`
    yaml = YAML(typ="rt")
    return yaml.load(StringIO(contents))  # type: ignore


def parse_yaml_preserve_spans(contents: str, filename: Optional[str]) -> YamlTree:
    """
    parse yaml into a YamlTree object. The resulting spans are tracked in SourceTracker
    so they can be used later when constructing error messages or displaying context.
    """
    # this uses the `RoundTripConstructor` which inherits from `SafeConstructor`
    source_hash = SourceTracker.add_source(contents)

    class SpanPreservingRuamelConstructor(RoundTripConstructor):
        def construct_object(self, node: Node, deep: bool = False) -> YamlTree:
            r = super().construct_object(node, deep)
            if isinstance(r, dict):
                r = YamlMap(r)
            return YamlTree(
                r, Span.from_node(node, source_hash=source_hash, filename=filename)
            )

    yaml = YAML()
    yaml.Constructor = SpanPreservingRuamelConstructor
    data = yaml.load(StringIO(contents))
    if not isinstance(data, YamlTree):
        raise Exception(
            f"Something went wrong parsing Yaml (expected a YamlTree as output): {PLEASE_FILE_ISSUE_TEXT}"
        )
    return data


EmptySpan = parse_yaml_preserve_spans("a: b", None).span
