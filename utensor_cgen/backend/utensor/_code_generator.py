# -*- coding:utf8 -*-
import logging
import os
import pickle
from tempfile import NamedTemporaryFile

import numpy as np

import tensorflow as tf
from tensorflow.core.framework.graph_pb2 import GraphDef
from tensorflow.tools.graph_transforms import TransformGraph
from utensor_cgen.backend.base import BackendPart
from utensor_cgen.frontend import FrontendSelector
from utensor_cgen.ir import uTensorGraph
from utensor_cgen.transformer.optimizer import RefCntOptimizer
from utensor_cgen.transformer.pipeline import TransformerPipeline
from utensor_cgen.utils import (NamescopedKWArgsParser, class_property,
                                parse_toml)

from ._operators import OperatorFactory
from .snippets import (CommentSnippet, ContextGlobalArrayContainer,
                       ContextHeaderSnippet, ContextSnippetsContainer,
                       CreateTensorBinarySnippet, CreateTensorIdxSnippet)
from .snippets.composer import Composer

__all__ = ["uTensorCodeGenerator"]
_logger = logging.getLogger('utensor-cli')

class uTensorCodeGenerator(BackendPart, object):

  TARGET = 'utensor'
  PART = 'code_generator'

  def __init__(
    self,
    config
  ):
    final_config = type(self).default_config
    final_config.update(config)
    self.src_fname = final_config['src_fname']
    self.params_dir = final_config['params_dir'].rstrip('/')
    if not os.path.exists(self.params_dir):
      os.makedirs(self.params_dir)
    self.embed_data_dir = final_config['embed_params_dir'].rstrip('/')
    self.model_dir = final_config['model_dir'].rstrip('/')
    self.trans_methods = final_config['transform_methods']
    self.save_graph = final_config['save_graph']
    self.debug_cmt = final_config['debug_cmt']

  @classmethod
  def from_kwargs(cls, **kwargs):
    return cls(config=kwargs)

  def apply(self, ugraph):
    """Generate source and header files
    """
    src_fname = self.src_fname
    if src_fname == 'None':
      src_fname = '{}.cpp'.format(ugraph.name)
    header_snippet = ContextHeaderSnippet(
      '_MODELS_{}'.format(ugraph.name),
      ugraph.name
    )
    weight_container = ContextGlobalArrayContainer()
    composer = Composer()
    header_fname = '{}.hpp'.format(ugraph.name)
    weight_header_fname = '{}_weight.hpp'.format(ugraph.name)
    container = ContextSnippetsContainer(ugraph.name, header_fname, weight_header_fname)

    opFactory = OperatorFactory()

    self._check_non_quantized(ugraph)
    _logger.info("Transforming graph: %s", ugraph.name)
    _logger.info("Transform pipeline: %s", ' -> '.join(self.trans_methods))
    quant_ugraph = self._transform_graph(ugraph,
                                         self.trans_methods)
    _logger.info('Graph transormation done')

    if self.save_graph:
      _logger.info('Saving transformed graph')
      pkl_fname = "quant_{}.pkl".format(ugraph.name)
      with open(pkl_fname, 'wb') as fid:
        pickle.dump(quant_ugraph, fid)
      _logger.info('{} saved'.format(pkl_fname))

    if not os.path.exists(os.path.join(self.params_dir, ugraph.name)):
      os.makedirs(os.path.join(self.params_dir, ugraph.name))
    for op_id, op_name in enumerate(quant_ugraph.topo_order):
      op_info = quant_ugraph.ops_info[op_name]
      op_type = op_info.op_type
      # TODO: better abstraction for snippet
      if op_type == "Placeholder":
        parser = NamescopedKWArgsParser(RefCntOptimizer.KWARGS_NAMESCOPE, 
                                        op_info.op_attr)
        out_tname = op_info.output_tensors[0].name
        ref_count = parser.get('ref_counts', [0])[0]
        container.template_vars["placeholders"].append(out_tname)
        container.template_vars["ref_counts"].append(ref_count)
        header_snippet.template_vars["placeholders"].append(out_tname)
      else:
        # TODO: the operator may correspond to multiple snippets (such as InlinTensor)
        # weight_container is passed to function for workaround
        snippet = opFactory.createOperatorSnippet(op_info,
                                                  idx_dir=os.path.join(self.params_dir, ugraph.name),
                                                  embed_data_dir=self.embed_data_dir,
                                                  weight_container=weight_container,
                                                  data_manager=quant_ugraph.data_manager)
        container.add_snippet(snippet)

      if self.debug_cmt:
        comments = ["<<< Operation id {}: {}".format(op_id, op_name),
                    ">>> Operation id {}: {}".format(op_id + 1, op_name)]
        cmt_snippet = CommentSnippet(comments)
        container.add_snippet(cmt_snippet)
    composer.add_snippet(container)

    # generate cpp/hpp files
    if not os.path.exists(self.model_dir):
      os.makedirs(self.model_dir)
    if any([method == 'inline' for method in self.trans_methods]):  
      _logger.info("Generate weight file: %s", weight_header_fname)
      with open(os.path.join(self.model_dir, weight_header_fname), "w") as wf:
        wf.write('// Auto generated by utensor-cli\n\n')
        wf.write(weight_container.render())
    else:
      container.remove_header('"{}"'.format(weight_header_fname))
      
    _logger.info("Generate header file: %s", header_fname)
    with open(os.path.join(self.model_dir, header_fname), "w") as wf:
      wf.write('// Auto generated by utensor-cli\n\n')
      wf.write(header_snippet.render())
    _logger.info("Generate source file: %s", src_fname)
    with open(os.path.join(self.model_dir, src_fname), "w") as wf:
      wf.write('// Auto generated by utensor-cli\n\n')
      wf.write(composer.compose())

  @class_property
  def default_config(cls):
    config = {}
    config['src_fname'] = 'None'
    config['params_dir'] = 'data'
    config['embed_params_dir'] = '/fs/data'
    config['model_dir'] = 'models'
    config['transform_methods'] = [
      'dropout(name_pattern=r"(dropout[_\w\d]*)/.*")',
      'linear_reorder',
      'quantize',
      'conv_pool',
      'inline',
      'biasAdd',
      'remove_id_op',
      'fake_gather_v2',
      'refcnt'
    ]
    config['save_graph'] = False
    config['debug_cmt'] = False
    return config
  
  @classmethod
  def _check_non_quantized(cls, ugraph):
    is_quantized = False
    for op_info in ugraph.ops_info.values():
      if op_info.op_type in [
        "Dequantize", "QuantizedMaxPool",
        "QuantizeV2", "QuantizedMatMul",
        "QuantizedRelu", "QuantizedAdd",
        "RequantizationRange",
        "Requantize",
        "QuantizedReshape",
        "QuantizedConv2D"
        ]:
        is_quantized = True
        break
    if is_quantized:
      _logger.warning(("Expecting non-quantized graph, "
                        "graph transformation/optimization might not work properly"))

  def _transform_graph(self, ugraph, methods):
    pipeline = TransformerPipeline(methods)
    return pipeline.transform(ugraph)

  def _tf_load_graph_def(self, pb_fname):
    with tf.gfile.FastGFile(pb_fname, 'rb') as fid:
      graph_def = tf.GraphDef()
      graph_def.ParseFromString(fid.read())
    return graph_def
