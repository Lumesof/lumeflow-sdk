from __future__ import annotations

from bazel.lore.fixture_operator_bin_impage_descriptor import (
    _IMAGE_DESCRIPTOR as _FIXTURE_OPERATOR_IMAGE_DESCRIPTOR,
)
from lumesof.lumeflow import FlowGraph
from lumesof.lumeflow import Graph
from lumesof.lumeflow import graph_type
from lumesof.lumeflow import materialize


@graph_type("async")
class FixtureGraph(Graph):
    @materialize
    def buildDag(self):
        flow = FlowGraph(name="fixture-graph")
        operator = flow.createOperatorFromImageDescriptor(
            name="fixture-op",
            descriptor=_FIXTURE_OPERATOR_IMAGE_DESCRIPTOR,
        )
        payload = flow.protoPayload("bazel.lore.tests.FixturePayload")
        flow.getLink(name="input").setLinkType(linkType="ASYNC_INJECTOR").addConsumer(
            operator,
            port="in",
            payload_type=payload,
        )
        return flow.createDag()
