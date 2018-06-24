# -*- coding:utf8 -*-
r"""Namescope Transformer

Transformers that get rid of namescope/nodes which are not needed 
for inference
"""
from collections import defaultdict
from copy import deepcopy
import re

from utensor_cgen.ir import uTensorGraph, OperationInfo
from utensor_cgen.utils import parse_tensor_name
from .base import Transformer

__all__ = ["DropoutTransformer", "BatchNormTransformer", "InlineTransformer"]

class InlineTransformer(Transformer):
  KWARGS_NAMESCOPE = 'inline'
  TARGET_NODENAME_PATTERN = re.compile(r'(const[_\w\d]*)/.*')


  def transform(self, ugraph):
    #self.prune_graph = False
    for node_name in ugraph.topo_order:
      op_type = ugraph.ops_info[node_name].op_type
      if op_type == 'Const':
        op_info = ugraph.ops_info[node_name]
        op_info.op_type = 'Inline'
    
    return ugraph

class DropoutTransformer(Transformer):
  """Remove Dropout Op
  """
  KWARGS_NAMESCOPE = 'dropout'
  TARGET_NODENAME_PATTERN = re.compile(r'(dropout[_\w\d]*)/.*')

  def transform(self, ugraph):

    dropout_input_map = self._find_input(ugraph)
    new_ops_info = {}
    new_topo_order = []
    for node_name in ugraph.topo_order:
      match = self.TARGET_NODENAME_PATTERN.match(node_name)
      if match:
        # ignore all dropout nodes
        continue
      # replace inputs with dropout inputs
      op_info = ugraph.ops_info[node_name]
      in_t_infos = [deepcopy(t_info) for t_info in op_info.input_tensors]
      out_t_infos = [deepcopy(t_info) for t_info in op_info.output_tensors]
      op_attr = deepcopy(op_info.op_attr)
      for i, t_info in enumerate(in_t_infos):
        op_name = parse_tensor_name(t_info.name)[0]
        match = self.TARGET_NODENAME_PATTERN.match(op_name)
        if match:
          name_scope = match.group(1)
          # assume there should be only on input except keep_prob
          dropout_in = dropout_input_map[name_scope].input_tensors[0]
          in_t_infos.pop(i)
          in_t_infos.insert(i, dropout_in)
      new_op_info = OperationInfo(name=op_info.name,
                                  input_tensors=in_t_infos,
                                  output_tensors=out_t_infos,
                                  op_type=op_info.op_type,
                                  backend=op_info.backend,
                                  op_attr=op_attr)
      new_ops_info[node_name] = new_op_info
      new_topo_order.append(node_name)
    new_graph = uTensorGraph()
    new_graph.ops_info = new_ops_info
    new_graph.topo_order = new_topo_order
    new_graph.output_nodes = deepcopy(ugraph.output_nodes)
    new_graph._backend = ugraph._backend
    return new_graph

  def _find_dropout_clusters(self, ugraph):
    clusters = defaultdict(lambda: [])
    for node_name in ugraph.topo_order:
      match = self.TARGET_NODENAME_PATTERN.match(node_name)
      if match:
        name_scope = match.group(1)
        clusters[name_scope].append(node_name)
    return dict(clusters)

  def _find_input(self, ugraph):
    """dropout_name --> input_op_info
    """
    clusters = self._find_dropout_clusters(ugraph)
    input_map = {}
    for node_name in ugraph.topo_order:
      match = self.TARGET_NODENAME_PATTERN.match(node_name)
      if match:
        name_scope = match.group(1)
        cluster = clusters[name_scope]
        op_info = ugraph.ops_info[node_name]
        in_op_names = [parse_tensor_name(in_tensor.name)[0] for in_tensor in op_info.input_tensors]
        for in_op_name in in_op_names:
          if in_op_name not in cluster and not in_op_name.startswith('keep_prob'):
            input_map[name_scope] = ugraph.ops_info[in_op_name]
    return input_map


class BatchNormTransformer(Transformer):
  """Replace Batch Norm namescope with uTensor Op
  """
  KWARGS_NAMESCOPE = 'batch_norm'
  TARGET_NODENAME_PATTERN = re.compile(r'(BatchNorm[_\w\d]*)/.*')

  def transform(self, ugraph):
    # TODO: implement this!
    pass
