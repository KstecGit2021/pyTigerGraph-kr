"""Data Loaders
:description: Data loader classes in the pyTigerGraph GDS module. 

Data loaders are classes in the pyTigerGraph Graph Data Science (GDS) module. 
You can define an instance of each data loader class through a link:https://docs.tigergraph.com/pytigergraph/current/gds/factory-functions[factory function].

Requires `querywriters` user permissions for full functionality. 
"""

import io
import logging
import math
import os
from queue import Empty, Queue
from threading import Event, Thread
from time import sleep
from typing import TYPE_CHECKING, Any, Iterator, NoReturn, Union, Tuple

if TYPE_CHECKING:
    from ..pyTigerGraph import TigerGraphConnection
    from kafka import KafkaAdminClient, KafkaConsumer
    import torch
    import dgl
    import torch_geometric as pyg

import numpy as np
import pandas as pd

from ..pyTigerGraphException import TigerGraphException
from .utilities import install_query_file, random_string

__all__ = ["VertexLoader", "EdgeLoader", "NeighborLoader", "GraphLoader"]
__pdoc__ = {}

_udf_funcs = {
    "INT": "int_to_string",
    "BOOL": "bool_to_string",
    "FLOAT": "float_to_string",
    "DOUBLE": "float_to_string",
}


class BaseLoader:
    """NO DOC: Base Dataloader Class."""
    def __init__(
        self,
        graph: "TigerGraphConnection",
        loaderID: str = None,
        numBatches: int = 1,
        bufferSize: int = 4,
        outputFormat: str = "dataframe",
        kafkaAddress: str = "",
        KafkaMaxMsgSize: int = 104857600,
        kafkaNumPartitions: int = 1,
        kafkaReplicaFactor: int = 1,
        kafkaRetentionMS: int = 60000,
        kafkaAutoDelTopic: bool = True,
        kafkaAddressForConsumer: str = None,
        kafkaAddressForProducer: str = None,
        timeout: int = 300000,
    ) -> None:
        """Base Class for data loaders.

        The job of a data loader is to stream data from the TigerGraph database to the client.
        Kafka is used as the data streaming pipeline. Hence, for the data loader to work,
        a running Kafka cluster is required.

        NOTE: When you initialize the loader on a graph for the first time,
        the initialization might take a minute as it installs the corresponding
        query to the database. However, the query installation only
        needs to be done once, so it will take no time when you initialize the loader
        on the same graph again.

        Args:
            graph (TigerGraphConnection):
                Connection to the TigerGraph database.
            loaderID (str):
                An identifier of the loader which can be any string. It is
                also used as the Kafka topic name. If `None`, a random string
                will be generated for it. Defaults to None.
            numBatches (int):
                Number of batches to divide the desired data into. Defaults to 1.
            bufferSize (int):
                Number of data batches to prefetch and store in memory. Defaults to 4.
            outputFormat (str):
                Format of the output data of the loader. Defaults to dataframe.
            kafkaAddress (str):
                Address of the kafka broker. Defaults to localhost:9092.
            maxKafkaMsgSize (int, optional):
                Maximum size of a Kafka message in bytes.
                Defaults to 104857600.
            kafkaNumPartitions (int, optional):
                Number of partitions for the topic created by this loader.
                Defaults to 1.
            kafkaReplicaFactor (int, optional):
                Number of replications for the topic created by this loader. 
                Defaults to 1.
            kafkaRetentionMS (int, optional):
                Retention time for messages in the topic created by this
                loader in milliseconds. Defaults to 60000.
            kafkaAutoDelTopic (bool, optional):
                Whether to delete the Kafka topic once the 
                loader finishes pulling data. Defaults to True.
            kafkaAddressForConsumer (str, optional):
                Address of the kafka broker that a consumer
                should use. Defaults to be the same as `kafkaAddress`.
            kafkaAddressForProducer (str, optional):
                Address of the kafka broker that a producer
                should use. Defaults to be the same as `kafkaAddress`.
            timeout (int, optional):
                Timeout value for GSQL queries, in ms. Defaults to 300000.
        """
        # Get graph info
        self._graph = graph
        self._v_schema, self._e_schema = self._get_schema()
        # Initialize basic params
        if not loaderID:
            self.loader_id = random_string(6)
        else:
            self.loader_id = loaderID
        self.num_batches = numBatches
        self.output_format = outputFormat
        self.buffer_size = bufferSize
        self.timeout = timeout
        self._iterations = 0
        self._iterator = False
        # Kafka consumer and admin
        self.max_kafka_msg_size = KafkaMaxMsgSize
        self.kafka_address_consumer = (
            kafkaAddressForConsumer if kafkaAddressForConsumer else kafkaAddress
        )
        self.kafka_address_producer = (
            kafkaAddressForProducer if kafkaAddressForProducer else kafkaAddress
        )
        if self.kafka_address_consumer:
            try:
                from kafka import KafkaAdminClient, KafkaConsumer
            except ImportError:
                raise ImportError("kafka-python is not installed. Please install it to use kafka streaming.")
            try:
                self._kafka_consumer = KafkaConsumer(
                    bootstrap_servers=self.kafka_address_consumer,
                    client_id=self.loader_id,
                    max_partition_fetch_bytes=KafkaMaxMsgSize,
                    fetch_max_bytes=KafkaMaxMsgSize,
                    auto_offset_reset="earliest"
                )
                self._kafka_admin = KafkaAdminClient(
                    bootstrap_servers=self.kafka_address_consumer,
                    client_id=self.loader_id,
                )
            except:
                raise ConnectionError(
                    "Cannot reach Kafka broker. Please check Kafka settings."
                )
        self.kafka_partitions = kafkaNumPartitions
        self.kafka_replica = kafkaReplicaFactor
        self.kafka_retention_ms = kafkaRetentionMS
        self.delete_kafka_topic = kafkaAutoDelTopic
        # Thread to send requests, download and load data
        self._requester = None
        self._downloader = None
        self._reader = None
        # Queues to store tasks and data
        self._request_task_q = None
        self._download_task_q = None
        self._read_task_q = None
        self._data_q = None
        self._kafka_topic = None
        # Exit signal to terminate threads
        self._exit_event = None
        # In-memory data cache. Only used if num_batches=1
        self._data = None
        # Default mode of the loader is for training
        self._mode = "training"
        # Implement `_install_query()` that installs your query
        # self._install_query()

    def __del__(self) -> NoReturn:
        self._reset()

    def _get_schema(self) -> Tuple[dict, dict]:
        v_schema = {}
        e_schema = {}
        schema = self._graph.getSchema()
        # Get vertex schema
        for vtype in schema["VertexTypes"]:
            v = vtype["Name"]
            v_schema[v] = {}
            for attr in vtype["Attributes"]:
                if "ValueTypeName" in attr["AttributeType"]:
                    v_schema[v][attr["AttributeName"]] = attr["AttributeType"][
                        "ValueTypeName"
                    ]
                else:
                    v_schema[v][attr["AttributeName"]] = attr["AttributeType"]["Name"]
            if vtype["PrimaryId"]["PrimaryIdAsAttribute"]:
                v_schema[v][vtype["PrimaryId"]["AttributeName"]] = vtype["PrimaryId"][
                    "AttributeType"
                ]["Name"]
        # Get edge schema
        for etype in schema["EdgeTypes"]:
            e = etype["Name"]
            e_schema[e] = {}
            for attr in etype["Attributes"]:
                if "ValueTypeName" in attr["AttributeType"]:
                    e_schema[e][attr["AttributeName"]] = attr["AttributeType"][
                        "ValueTypeName"
                    ]
                else:
                    e_schema[e][attr["AttributeName"]] = attr["AttributeType"]["Name"]
        return v_schema, e_schema

    def _validate_vertex_attributes(
        self, attributes: Union[list, dict]
    ) -> Union[list, dict]:
        if not attributes:
            return []
        if isinstance(attributes, str):
            raise ValueError(
                "The old string way of specifying attributes is deprecated to better support heterogeneous graphs. Please use the new format."
            )
        if isinstance(attributes, list):
            for i in range(len(attributes)):
                attributes[i] = attributes[i].strip()
            attr_set = set(attributes)
            for vtype in self._v_schema:
                allowlist = set(self._v_schema[vtype].keys())
                if attr_set - allowlist:
                    raise ValueError(
                        "Not all attributes are available for vertex type {}.".format(
                            vtype
                        )
                    )
        elif isinstance(attributes, dict):
            # Wait for the heterogeneous graph support
            for vtype in attributes:
                if vtype not in self._v_schema:
                    raise ValueError(
                        "Vertex type {} is not available in the database.".format(vtype)
                    )
                for i in range(len(attributes[vtype])):
                    attributes[vtype][i] = attributes[vtype][i].strip()
                attr_set = set(attributes[vtype])
                allowlist = set(self._v_schema[vtype].keys())
                if attr_set - allowlist:
                    raise ValueError(
                        "Not all attributes are available for vertex type {}.".format(
                            vtype
                        )
                    )
            raise NotImplementedError
        return attributes

    def _validate_edge_attributes(
        self, attributes: Union[list, dict]
    ) -> Union[list, dict]:
        if not attributes:
            return []
        if isinstance(attributes, str):
            raise ValueError(
                "The old string way of specifying attributes is deprecated to better support heterogeneous graphs. Please use the new format."
            )
        if isinstance(attributes, list):
            for i in range(len(attributes)):
                attributes[i] = attributes[i].strip()
            attr_set = set(attributes)
            for etype in self._e_schema:
                allowlist = set(self._e_schema[etype].keys())
                if attr_set - allowlist:
                    raise ValueError(
                        "Not all attributes are available for edge type {}.".format(
                            etype
                        )
                    )
        elif isinstance(attributes, dict):
            # Wait for the heterogeneous graph support
            for etype in attributes:
                if etype not in self._e_schema:
                    raise ValueError(
                        "Edge type {} is not available in the database.".format(etype)
                    )
                for i in range(len(attributes[etype])):
                    attributes[etype][i] = attributes[etype][i].strip()
                attr_set = set(attributes[etype])
                allowlist = set(self._v_schema[etype].keys())
                if attr_set - allowlist:
                    raise ValueError(
                        "Not all attributes are available for edge type {}.".format(
                            etype
                        )
                    )
            raise NotImplementedError
        return attributes

    def _install_query(self) -> NoReturn:
        # Install the right GSQL query for the loader.
        self.query_name = ""
        raise NotImplementedError

    @staticmethod
    def _request_kafka(
        exit_event: Event,
        tgraph: "TigerGraphConnection",
        query_name: str,
        kafka_consumer: "KafkaConsumer",
        kafka_admin: "KafkaAdminClient",
        kafka_topic: str,
        kafka_partitions: int = 1,
        kafka_replica: int = 1,
        kafka_topic_size: int = 100000000,
        kafka_retention_ms: int = 60000,
        timeout: int = 600000,
        payload: dict = {},
        headers: dict = {},
    ) -> NoReturn:
        # Create topic if not exist
        try:
            from kafka.admin import NewTopic
        except ImportError:
            raise ImportError("kafka-python is not installed. Please install it to use kafka streaming.")
        if kafka_topic not in kafka_consumer.topics():
            new_topic = NewTopic(
                kafka_topic,
                kafka_partitions,
                kafka_replica,
                topic_configs={
                    "retention.ms": str(kafka_retention_ms),
                    "max.message.bytes": str(kafka_topic_size),
                },
            )
            resp = kafka_admin.create_topics([new_topic])
            if resp.to_object()["topic_errors"][0]["error_code"] != 0:
                raise ConnectionError(
                    "Failed to create Kafka topic {} at {}.".format(
                        kafka_topic, kafka_consumer.config["bootstrap_servers"]
                    )
                )
        # Subscribe to the topic
        kafka_consumer.subscribe([kafka_topic])
        _ = kafka_consumer.topics() # Call this to refresh metadata. Or the new subscription seems to be delayed.
        # Run query async
        # TODO: change to runInstalledQuery when it supports async mode
        _headers = {"GSQL-ASYNC": "true", "GSQL-TIMEOUT": str(timeout)}
        _headers.update(headers)
        _payload = {}
        _payload.update(payload)
        resp = tgraph._post(
            tgraph.restppUrl + "/query/" + tgraph.graphname + "/" + query_name,
            data=_payload,
            headers=_headers,
            resKey=None
        )
        # Check status
        _stat_payload = {
            "graph_name": tgraph.graphname,
            "requestid": resp["request_id"],
        }
        while not exit_event.is_set():
            status = tgraph._get(
                tgraph.restppUrl + "/query_status", params=_stat_payload
            )
            if status[0]["status"] == "running":
                sleep(1)
                continue
            elif status[0]["status"] == "success":
                res = tgraph._get(
                    tgraph.restppUrl + "/query_result", params=_stat_payload
                )
                if res[0]["kafkaError"]:
                    raise TigerGraphException(
                        "Error writing to Kafka: {}".format(res[0]["kafkaError"])
                    )
                else:
                    break
            else:
                raise TigerGraphException(
                    "Error generating data. Query {}.".format(
                        status["results"][0]["status"]
                    )
                )

    @staticmethod
    def _request_rest(
        tgraph: "TigerGraphConnection",
        query_name: str,
        read_task_q: Queue,
        timeout: int = 600000,
        payload: dict = {},
        resp_type: str = "both",
    ) -> NoReturn:
        # Run query
        resp = tgraph.runInstalledQuery(
            query_name, params=payload, timeout=timeout, usePost=True
        )
        # Put raw data into reading queue
        for i in resp:
            if resp_type == "both":
                data = ("".join(i["vertex_batch"].values()), i["edge_batch"])
            elif resp_type == "vertex":
                data = "".join(i["vertex_batch"].values())
            elif resp_type == "edge":
                data = i["edge_batch"]
            read_task_q.put(data)
        read_task_q.put(None)

    @staticmethod
    def _download_from_kafka(
        exit_event: Event,
        read_task_q: Queue,
        num_batches: int,
        out_tuple: bool,
        kafka_consumer: "KafkaConsumer",
    ) -> NoReturn:
        delivered_batch = 0
        buffer = {}
        while not exit_event.is_set():
            if delivered_batch == num_batches:
                break
            resp = kafka_consumer.poll(1000)
            if not resp:
                continue
            for msgs in resp.values():
                for message in msgs:
                    key = message.key.decode("utf-8")
                    if out_tuple:
                        if key.startswith("vertex"):
                            companion_key = key.replace("vertex", "edge")
                            if companion_key in buffer:
                                read_task_q.put((message.value, buffer[companion_key]))
                                del buffer[companion_key]
                                delivered_batch += 1
                            else:
                                buffer[key] = message.value
                        elif key.startswith("edge"):
                            companion_key = key.replace("edge", "vertex")
                            if companion_key in buffer:
                                read_task_q.put((buffer[companion_key], message.value))
                                del buffer[companion_key]
                                delivered_batch += 1
                            else:
                                buffer[key] = message.value
                        else:
                            raise ValueError(
                                "Unrecognized key {} for messages in kafka".format(key)
                            )
                    else:
                        read_task_q.put(message.value)
                        delivered_batch += 1
        read_task_q.put(None)

    @staticmethod
    def _read_data(
        exit_event: Event,
        in_q: Queue,
        out_q: Queue,
        in_format: str = "vertex_bytes",
        out_format: str = "dataframe",
        v_in_feats: Union[list, dict] = [],
        v_out_labels: Union[list, dict] = [],
        v_extra_feats: Union[list, dict] = [],
        v_attr_types: dict = {},
        e_in_feats: Union[list, dict] = [],
        e_out_labels: Union[list, dict] = [],
        e_extra_feats: Union[list, dict] = [],
        e_attr_types: dict = {},
        add_self_loop: bool = False,
        reindex: bool = True,
    ) -> NoReturn:
        while not exit_event.is_set():
            raw = in_q.get()
            if raw is None:
                in_q.task_done()
                out_q.put(None)
                break
            data = BaseLoader._parse_data(
                raw = raw,
                in_format = in_format,
                out_format = out_format,
                v_in_feats = v_in_feats,
                v_out_labels = v_out_labels,
                v_extra_feats = v_extra_feats,
                v_attr_types = v_attr_types,
                e_in_feats = e_in_feats,
                e_out_labels = e_out_labels,
                e_extra_feats = e_extra_feats,
                e_attr_types = e_attr_types,
                add_self_loop = add_self_loop,
                reindex = reindex,
                primary_id = []
            )
            out_q.put(data)
            in_q.task_done()

    @staticmethod
    def _parse_data(
        raw: Union[str, bytes, Tuple[str, str], Tuple[bytes, bytes]],
        in_format: str = "vertex_bytes",
        out_format: str = "dataframe",
        v_in_feats: Union[list, dict] = [],
        v_out_labels: Union[list, dict] = [],
        v_extra_feats: Union[list, dict] = [],
        v_attr_types: dict = {},
        e_in_feats: Union[list, dict] = [],
        e_out_labels: Union[list, dict] = [],
        e_extra_feats: Union[list, dict] = [],
        e_attr_types: dict = {},
        add_self_loop: bool = False,
        reindex: bool = True,
        primary_id: list = []
    ) -> Union[pd.DataFrame, Tuple[pd.DataFrame, pd.DataFrame], "dgl.DGLGraph", "pyg.Data"]:
        def attr_to_tensor(
            attributes: list, attr_types: dict, df: pd.DataFrame
        ) -> "torch.Tensor":
            x = []
            for col in attributes:
                dtype = attr_types[col].lower()
                if dtype.startswith("str"):
                    raise TypeError(
                        "String type not allowed for input and output features."
                    )
                if df[col].dtype == "object":
                    x.append(df[col].str.split(expand=True).to_numpy().astype(dtype))
                else:
                    x.append(df[[col]].to_numpy().astype(dtype))
            return torch.tensor(np.hstack(x).squeeze())

        v_attributes = ["vid"] + v_in_feats + v_out_labels + v_extra_feats
        e_attributes = ["source", "target"] + e_in_feats + e_out_labels + e_extra_feats

        vertices, edges = None, None
        if in_format == "vertex_bytes":
            # Bytes of vertices in format vid,v_in_feats,v_out_labels,v_extra_feats
            data = pd.read_csv(io.BytesIO(raw), header=None, names=v_attributes)
        elif in_format == "edge_bytes":
            # Bytes of edges in format source_vid,target_vid
            data = pd.read_csv(io.BytesIO(raw), header=None, names=e_attributes)
        elif in_format == "graph_bytes":
            # A pair of in-memory CSVs (vertex, edge)
            v_file, e_file = raw
            vertices = pd.read_csv(
                io.BytesIO(v_file), header=None, names=v_attributes
            )
            edges = pd.read_csv(io.BytesIO(e_file), header=None, names=e_attributes)
            data = (vertices, edges)
        elif in_format == "vertex_str":
            # String of vertices in format vid,v_in_feats,v_out_labels,v_extra_feats
            data = pd.read_csv(io.StringIO(raw), header=None, names=v_attributes)
        elif in_format == "edge_str":
            # String of edges in format source_vid,target_vid
            data = pd.read_csv(io.StringIO(raw), header=None, names=e_attributes)
        elif in_format == "graph_str":
            # A pair of in-memory CSVs (vertex, edge)
            v_file, e_file = raw
            vertices = pd.read_csv(
                io.StringIO(v_file), header=None, names=v_attributes
            )
            if primary_id:
                vertices["primary_id"] = primary_id
                v_extra_feats.append("primary_id")
            edges = pd.read_csv(
                io.StringIO(e_file), header=None, names=e_attributes
            )
            data = (vertices, edges)
        else:
            raise NotImplementedError

        if out_format.lower() == "pyg" or out_format.lower() == "dgl":
            try:
                import torch
            except ImportError:
                raise ImportError("PyTorch is not installed. Please install it to use PyG or DGL output.")
            if vertices is None or edges is None:
                raise ValueError(
                    "PyG or DGL format can only be used with graph output."
                )
            if out_format.lower() == "dgl":
                try:
                    import dgl
                    mode = "dgl"
                except ImportError:
                    raise ImportError(
                        "DGL is not installed. Please install DGL to use DGL format."
                    )
            elif out_format.lower() == "pyg":
                try:
                    from torch_geometric.data import Data as pygData
                    from torch_geometric.utils import add_self_loops
                    mode = "pyg"
                except ImportError:
                    raise ImportError(
                        "PyG is not installed. Please install PyG to use PyG format."
                    )
            else:
                raise NotImplementedError
            # Reformat as a graph.
            # Need to have a pair of tables for edges and vertices.
            # Deal with edgelist first
            if reindex:
                vertices["tmp_id"] = range(len(vertices))
                id_map = vertices[["vid", "tmp_id"]]
                edges = edges.merge(id_map, left_on="source", right_on="vid")
                edges.drop(columns=["source", "vid"], inplace=True)
                edges = edges.merge(id_map, left_on="target", right_on="vid")
                edges.drop(columns=["target", "vid"], inplace=True)
                edgelist = edges[["tmp_id_x", "tmp_id_y"]]
            else:
                edgelist = edges[["source", "target"]]
            edgelist = torch.tensor(edgelist.to_numpy().T, dtype=torch.long)
            if mode == "dgl":
                data = dgl.graph(data=(edgelist[0], edgelist[1]))
                if add_self_loop:
                    data = dgl.add_self_loop(data)
            elif mode == "pyg":
                data = pygData()
                if add_self_loop:
                    edgelist = add_self_loops(edgelist)[0]
                data["edge_index"] = edgelist
            del edgelist
            # Deal with edge attributes
            if e_in_feats:
                if mode == "dgl":
                    data.edata["feat"] = attr_to_tensor(
                        e_in_feats, e_attr_types, edges
                    )
                elif mode == "pyg":
                    data["edge_feat"] = attr_to_tensor(e_in_feats, e_attr_types, edges)
            if e_out_labels:
                if mode == "dgl":
                    data.edata["label"] = attr_to_tensor(
                        e_out_labels, e_attr_types, edges
                    )
                elif mode == "pyg":
                    data["edge_label"] = attr_to_tensor(e_out_labels, e_attr_types, edges)
            if e_extra_feats:
                if mode == "dgl":
                    data.extra_data = {}
                for col in e_extra_feats:
                    dtype = e_attr_types[col].lower()
                    if dtype.startswith("str"):
                        if mode == "dgl":
                            data.extra_data[col] = edges[col].to_list()
                        elif mode == "pyg":
                            data[col] = edges[col].to_list()
                    elif edges[col].dtype == "object":
                        if mode == "dgl":
                            data.edata[col] = torch.tensor(
                                edges[col]
                                .str.split(expand=True)
                                .to_numpy()
                                .astype(dtype)
                            )
                        elif mode == "pyg":
                            data[col] = torch.tensor(
                                edges[col]
                                .str.split(expand=True)
                                .to_numpy()
                                .astype(dtype)
                            )
                    else:
                        if mode == "dgl":
                            data.edata[col] = torch.tensor(
                                edges[col].to_numpy().astype(dtype)
                            )
                        elif mode == "pyg":
                            data[col] = torch.tensor(
                                edges[col].to_numpy().astype(dtype)
                            )
            del edges
            # Deal with vertex attributes next
            if v_in_feats:
                if mode == "dgl":
                    data.ndata["feat"] = attr_to_tensor(
                        v_in_feats, v_attr_types, vertices
                    )
                elif mode == "pyg":
                    data["x"] = attr_to_tensor(v_in_feats, v_attr_types, vertices)
            if v_out_labels:
                if mode == "dgl":
                    data.ndata["label"] = attr_to_tensor(
                        v_out_labels, v_attr_types, vertices
                    )
                elif mode == "pyg":
                    data["y"] = attr_to_tensor(v_out_labels, v_attr_types, vertices)
            if v_extra_feats:
                if mode == "dgl":
                    data.extra_data = {}
                for col in v_extra_feats:
                    dtype = v_attr_types[col].lower()
                    if dtype.startswith("str"):
                        if mode == "dgl":
                            data.extra_data[col] = vertices[col].to_list()
                        elif mode == "pyg":
                            data[col] = vertices[col].to_list()
                    elif vertices[col].dtype == "object":
                        if mode == "dgl":
                            data.ndata[col] = torch.tensor(
                                vertices[col]
                                .str.split(expand=True)
                                .to_numpy()
                                .astype(dtype)
                            )
                        elif mode == "pyg":
                            data[col] = torch.tensor(
                                vertices[col]
                                .str.split(expand=True)
                                .to_numpy()
                                .astype(dtype)
                            )
                    else:
                        if mode == "dgl":
                            data.ndata[col] = torch.tensor(
                                vertices[col].to_numpy().astype(dtype)
                            )
                        elif mode == "pyg":
                            data[col] = torch.tensor(
                                vertices[col].to_numpy().astype(dtype)
                            )
            del vertices
        elif out_format.lower() == "dataframe":
            pass
        else:
            raise NotImplementedError

        return data
        
    def _start(self) -> None:
        # This is a template. Implement your own logics here.
        # Create task and result queues
        self._request_task_q = Queue()
        self._read_task_q = Queue()
        self._data_q = Queue(self._buffer_size)
        self._exit_event = Event()

        # Start requesting thread. Finish with your logic.
        self._requester = Thread(target=self._request_kafka, args=())
        self._requester.start()

        # Start downloading thread. Finish with your logic.
        self._downloader = Thread(target=self._download_from_kafka, args=())
        self._downloader.start()

        # Start reading thread. Finish with your logic.
        self._reader = Thread(target=self._read_data, args=())
        self._reader.start()

        raise NotImplementedError

    def __iter__(self) -> Iterator:
        if self.num_batches == 1:
            return iter([self.data])
        self._reset()
        self._start()
        self._iterations += 1
        self._iterator = True
        return self

    def __next__(self) -> Any:
        if not self._iterator:
            raise TypeError(
                "Not an iterator. Call `iter` on it first or use it in a for loop."
            )
        if not self._data_q:
            self._iterator = False
            raise StopIteration
        data = self._data_q.get()
        if data is None:
            self._iterator = False
            raise StopIteration
        return data

    @property
    def data(self) -> Any:
        """A property of the instance. 
        The `data` property stores all data if all data is loaded in a single batch.
        If there are multiple batches of data, the `data` property returns the instance itself"""
        if self.num_batches == 1:
            if self._data is None:
                self._reset()
                self._start()
                self._data = self._data_q.get()
            return self._data
        else:
            return self

    def _reset(self) -> None:
        logging.debug("Resetting the loader")
        if self._exit_event:
            self._exit_event.set()
        if self._request_task_q:
            self._request_task_q.put(None)
        if self._download_task_q:
            self._download_task_q.put(None)
        if self._read_task_q:
            while True:
                try:
                    self._read_task_q.get(block=False)
                except Empty:
                    break
            self._read_task_q.put(None)
        if self._data_q:
            while True:
                try:
                    self._data_q.get(block=False)
                except Empty:
                    break
        if self._requester:
            self._requester.join()
        if self._downloader:
            self._downloader.join()
        if self._reader:
            self._reader.join()
        del self._request_task_q, self._download_task_q, self._read_task_q, self._data_q
        self._exit_event = None
        self._requester, self._downloader, self._reader = None, None, None
        self._request_task_q, self._download_task_q, self._read_task_q, self._data_q = (
            None,
            None,
            None,
            None,
        )
        if self.delete_kafka_topic:
            if self._kafka_topic:
                self._kafka_consumer.unsubscribe()
                resp = self._kafka_admin.delete_topics([self._kafka_topic])
                del_res = resp.to_object()["topic_error_codes"][0]
                if del_res["error_code"] != 0:
                    raise TigerGraphException(
                        "Failed to delete topic {}".format(del_res["topic"])
                    )
                self._kafka_topic = None
        logging.debug("Successfully reset the loader")

    def fetch(self, payload: dict) -> None:
        """Fetch the specific data instances for inference/prediction.

        Args:
            payload (dict): The JSON payload to send to the API.
        """
        if self._mode == "training":
            print(
                "Loader is in training mode. Please call `inference()` function to switch to inference mode."
            )

        # Send request
        # Parse data
        # Return data
        raise NotImplementedError


class NeighborLoader(BaseLoader):
    """NeighborLoader
    
    A data loader that performs neighbor sampling. 
    You can declare a `NeighborLoader` instance with the factory function `neighborLoder()`.
    
    A neighbor loader is an iterable.
    When you loop through a neighbor loader instance, it loads one batch of data from the graph to which you established a connection. 
    
    In every iteration, it first chooses a specified number of vertices as seeds,
    then picks a specified number of neighbors of each seed at random,
    then the same number of neighbors of each neighbor, and repeat for a specified number of hops.
    It loads both the vertices and the edges connecting them to their neighbors. 
    The vertices sampled this way along with their edges form one subgraph and is contained in one batch.

    You can iterate on the instance until every vertex has been picked as seed. 

    Examples:
    
    The following example iterates over a neighbor loader instance. 
    [.wrap,python]
    ----
    for i, batch in enumerate(neighbor_loader):
        print("----Batch {}----".format(i))
        print(batch)
    ----
    


    See https://github.com/TigerGraph-DevLabs/mlworkbench-docs/blob/1.0/tutorials/basics/3_neighborloader.ipynb[the ML Workbench tutorial notebook]
        for examples.
    See more details about the specific sampling method in 
    link:https://arxiv.org/abs/1706.02216[Inductive Representation Learning on Large Graphs].
    """
    def __init__(
        self,
        graph: "TigerGraphConnection",
        v_in_feats: Union[list, dict] = None,
        v_out_labels: Union[list, dict] = None,
        v_extra_feats: Union[list, dict] = None,
        e_in_feats: Union[list, dict] = None,
        e_out_labels: Union[list, dict] = None,
        e_extra_feats: Union[list, dict] = None,
        batch_size: int = None,
        num_batches: int = 1,
        num_neighbors: int = 10,
        num_hops: int = 2,
        shuffle: bool = False,
        filter_by: str = None,
        output_format: str = "PyG",
        add_self_loop: bool = False,
        loader_id: str = None,
        buffer_size: int = 4,
        kafka_address: str = None,
        kafka_max_msg_size: int = 104857600,
        kafka_num_partitions: int = 1,
        kafka_replica_factor: int = 1,
        kafka_retention_ms: int = 60000,
        kafka_auto_del_topic: bool = True,
        kafka_address_consumer: str = None,
        kafka_address_producer: str = None,
        timeout: int = 300000,
    ) -> None:
        """NO DOC"""
  
        super().__init__(
            graph,
            loader_id,
            num_batches,
            buffer_size,
            output_format,
            kafka_address,
            kafka_max_msg_size,
            kafka_num_partitions,
            kafka_replica_factor,
            kafka_retention_ms,
            kafka_auto_del_topic,
            kafka_address_consumer,
            kafka_address_producer,
            timeout,
        )
        # Resolve attributes
        self.v_in_feats = self._validate_vertex_attributes(v_in_feats)
        self.v_out_labels = self._validate_vertex_attributes(v_out_labels)
        self.v_extra_feats = self._validate_vertex_attributes(v_extra_feats)
        self.e_in_feats = self._validate_edge_attributes(e_in_feats)
        self.e_out_labels = self._validate_edge_attributes(e_out_labels)
        self.e_extra_feats = self._validate_edge_attributes(e_extra_feats)
        # Initialize parameters for the query
        self._payload = {}
        if batch_size:
            # If batch_size is given, calculate the number of batches
            num_vertices_by_type = self._graph.getVertexCount("*")
            if filter_by:
                num_vertices = sum(
                    self._graph.getVertexCount(k, where="{}!=0".format(filter_by))
                    for k in num_vertices_by_type
                )
            else:
                num_vertices = sum(num_vertices_by_type.values())
            self.num_batches = math.ceil(num_vertices / batch_size)
        else:
            # Otherwise, take the number of batches as is.
            self.num_batches = num_batches
        self._payload["num_batches"] = self.num_batches
        self._payload["num_neighbors"] = num_neighbors
        self._payload["num_hops"] = num_hops
        if filter_by:
            self._payload["filter_by"] = filter_by
        self._payload["shuffle"] = shuffle
        if self.kafka_address_producer:
            self._payload["kafka_address"] = self.kafka_address_producer
        # kafka_topic will be filled in later.
        # Output
        self.add_self_loop = add_self_loop
        # Install query
        self.query_name = self._install_query()

    def _install_query(self):
        # Install the right GSQL query for the loader.
        v_attr_names = self.v_in_feats + self.v_out_labels + self.v_extra_feats
        e_attr_names = self.e_in_feats + self.e_out_labels + self.e_extra_feats
        query_replace = {"{QUERYSUFFIX}": "_".join(v_attr_names+e_attr_names)}
        v_attr_types = next(iter(self._v_schema.values()))
        e_attr_types = next(iter(self._e_schema.values()))
        if v_attr_names:
            query_print = '+","+'.join(
                "{}(s.{})".format(_udf_funcs[v_attr_types[attr]], attr)
                for attr in v_attr_names
            )
            query_replace["{VERTEXATTRS}"] = query_print
        else:
            query_replace['+ "," + {VERTEXATTRS}'] = ""
        if e_attr_names:
            query_print = '+","+'.join(
                "{}(e.{})".format(_udf_funcs[e_attr_types[attr]], attr)
                for attr in e_attr_names
            )
            query_replace["{EDGEATTRS}"] = query_print
        else:
            query_replace['+ "," + {EDGEATTRS}'] = ""
        if self.kafka_address_producer:
            query_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "gsql",
                "dataloaders",
                "neighbor_kloader.gsql",
            )
        else:
            query_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "gsql",
                "dataloaders",
                "neighbor_hloader.gsql",
            )
        return install_query_file(self._graph, query_path, query_replace)

    def _start(self) -> None:
        # Create task and result queues
        self._read_task_q = Queue(self.buffer_size * 2)
        self._data_q = Queue(self.buffer_size)
        self._exit_event = Event()

        # Start requesting thread.
        if self.kafka_address_consumer:
            # If using kafka
            self._kafka_topic = "{}_{}".format(self.loader_id, self._iterations)
            self._payload["kafka_topic"] = self._kafka_topic
            self._requester = Thread(
                target=self._request_kafka,
                args=(
                    self._exit_event,
                    self._graph,
                    self.query_name,
                    self._kafka_consumer,
                    self._kafka_admin,
                    self._kafka_topic,
                    self.kafka_partitions,
                    self.kafka_replica,
                    self.max_kafka_msg_size,
                    self.kafka_retention_ms,
                    self.timeout,
                    self._payload,
                ),
            )
        else:
            # Otherwise, use rest api
            self._requester = Thread(
                target=self._request_rest,
                args=(
                    self._graph,
                    self.query_name,
                    self._read_task_q,
                    self.timeout,
                    self._payload,
                    "both",
                ),
            )
        self._requester.start()

        # If using Kafka, start downloading thread.
        if self.kafka_address_consumer:
            self._downloader = Thread(
                target=self._download_from_kafka,
                args=(
                    self._exit_event,
                    self._read_task_q,
                    self.num_batches,
                    True,
                    self._kafka_consumer,
                ),
            )
            self._downloader.start()

        # Start reading thread.
        v_attr_types = next(iter(self._v_schema.values()))
        v_attr_types["is_seed"] = "bool"
        e_attr_types = next(iter(self._e_schema.values()))
        if self.kafka_address_consumer:
            raw_format = "graph_bytes"
        else:
            raw_format = "graph_str"
        self._reader = Thread(
            target=self._read_data,
            args=(
                self._exit_event,
                self._read_task_q,
                self._data_q,
                raw_format,
                self.output_format,
                self.v_in_feats,
                self.v_out_labels,
                self.v_extra_feats + ["is_seed"],
                v_attr_types,
                self.e_in_feats,
                self.e_out_labels,
                self.e_extra_feats,
                e_attr_types,
                self.add_self_loop,
                True,
            ),
        )
        self._reader.start()
    
    @property
    def data(self) -> Any:
        """A property of the instance. 
        The `data` property stores all data if all data is loaded in a single batch.
        If there are multiple batches of data, the `data` property returns the instance itself"""
        return super().data

    def fetch(self, vertices: list) -> None:
        """Fetch neighborhood subgraphs for specific vertices.

        Args:
            vertices (list of dict): 
                Vertices to fetch with their neighborhood subgraphs. 
                Each vertex corresponds to a dict with two mandatory keys 
                {"primary_id": ..., "type": ...}
        """
        # Check input
        if not vertices:
            return None
        if not isinstance(vertices, list):
            raise ValueError('Input to fetch() should be in format: [{"primary_id": ..., "type": ...}, ...]')
        for i in vertices:
            if not (isinstance(i, dict) and len(i)==2):
                raise ValueError('Input to fetch() should be in format: [{"primary_id": ..., "type": ...}, ...]')
        # Send request
        _payload = {}
        _payload["num_batches"] = 1
        _payload["num_neighbors"] = self._payload["num_neighbors"]
        _payload["num_hops"] = self._payload["num_hops"]
        _payload["input_vertices"] = []
        for i in vertices:
            _payload["input_vertices"].append((i["primary_id"], i["type"]))
        resp = self._graph.runInstalledQuery(
            self.query_name, params=_payload, timeout=self.timeout, usePost=True
        )
        # Parse data
        v_attr_types = next(iter(self._v_schema.values()))
        v_attr_types["is_seed"] = "bool"
        v_attr_types["primary_id"] = "str"
        e_attr_types = next(iter(self._e_schema.values()))
        i = resp[0]
        data = self._parse_data(
            raw = ("".join(i["vertex_batch"].values()), i["edge_batch"]),
            in_format = "graph_str",
            out_format = self.output_format,
            v_in_feats = self.v_in_feats,
            v_out_labels = self.v_out_labels,
            v_extra_feats = self.v_extra_feats + ["is_seed"],
            v_attr_types = v_attr_types, 
            e_in_feats = self.e_in_feats,
            e_out_labels = self.e_out_labels,
            e_extra_feats = self.e_extra_feats,
            e_attr_types = e_attr_types,
            add_self_loop = self.add_self_loop,
            reindex = True,
            primary_id = list(i["vertex_batch"].keys())
        )
        # Return data
        return data

class EdgeLoader(BaseLoader):
    """Edge Loader.
    
    Data loader that loads all edges from the graph in batches.
    You can define an edge loader using the `edgeLoader()` factory function.

    An edge loader instance is an iterable. 
    When you loop through an edge loader instance, it loads one batch of data from the graph to which you established a connection in each iteration.
    The size and total number of batches are specified when you define the edge loader instance. 
    
    The boolean attribute provided to `filter_by` indicates which edges are included.
    If you need random batches, set `shuffle` to True.

    Examples:
    The following for loop prints every edge in batches. 

    [tabs]
    ====
    Input::
    +
    --
    [.wrap,python]
    ----
    edge_loader = conn.gds.edgeLoader(
        num_batches=10,
        attributes=["time", "is_train"],
        shuffle=True,
        filter_by=None
    )
    for i, batch in enumerate(edge_loader):
        print("----Batch {}: Shape {}----".format(i, batch.shape))
        print(batch.head(1))
    ----
    --
    Output::
    +
    --
    ----
    ----Batch 0: Shape (1129, 4)----
        source    target  time  is_train
    0  3145728  22020185     0         1
    ----Batch 1: Shape (1002, 4)----
        source    target  time  is_train
    0  1048577  20971586     0         1
    ----Batch 2: Shape (1124, 4)----
    source   target  time  is_train
    0       4  9437199     0         1
    ----Batch 3: Shape (1071, 4)----
        source    target  time  is_train
    0  11534340  32505859     0         1
    ----Batch 4: Shape (978, 4)----
        source    target  time  is_train
    0  11534341  16777293     0         1
    ----Batch 5: Shape (1149, 4)----
        source   target  time  is_train
    0  5242882  2097158     0         1
    ----Batch 6: Shape (1013, 4)----
        source    target  time  is_train
    0  4194305  23068698     0         1
    ----Batch 7: Shape (1037, 4)----
        source   target  time  is_train
    0  7340035  4194337     0         0
    ----Batch 8: Shape (1067, 4)----
    source   target  time  is_train
    0       3  1048595     0         1
    ----Batch 9: Shape (986, 4)----
        source    target  time  is_train
    0  9437185  13631508     0         1
    ----
    --
    ====


    See https://github.com/TigerGraph-DevLabs/mlworkbench-docs/blob/1.0/tutorials/basics/3_edgeloader.ipynb[the ML Workbench edge loader tutorial notebook]
        for examples.
    """
    def __init__(
        self,
        graph: "TigerGraphConnection",
        attributes: Union[list, dict] = None,
        batch_size: int = None,
        num_batches: int = 1,
        shuffle: bool = False,
        filter_by: str = None,
        output_format: str = "dataframe",
        loader_id: str = None,
        buffer_size: int = 4,
        kafka_address: str = None,
        kafka_max_msg_size: int = 104857600,
        kafka_num_partitions: int = 1,
        kafka_replica_factor: int = 1,
        kafka_retention_ms: int = 60000,
        kafka_auto_del_topic: bool = True,
        kafka_address_consumer: str = None,
        kafka_address_producer: str = None,
        timeout: int = 300000,
    ) -> None:
        """
        NO DOC.
        """
        super().__init__(
            graph,
            loader_id,
            num_batches,
            buffer_size,
            output_format,
            kafka_address,
            kafka_max_msg_size,
            kafka_num_partitions,
            kafka_replica_factor,
            kafka_retention_ms,
            kafka_auto_del_topic,
            kafka_address_consumer,
            kafka_address_producer,
            timeout,
        )
        # Resolve attributes
        self.attributes = self._validate_edge_attributes(attributes)
        # Initialize parameters for the query
        self._payload = {}
        if batch_size:
            # If batch_size is given, calculate the number of batches
            num_edges_by_type = self._graph.getEdgeCount("*")
            if filter_by:
                # TODO: get edge count with filter
                raise NotImplementedError
            else:
                num_edges = sum(num_edges_by_type.values())
            self.num_batches = math.ceil(num_edges / batch_size)
        else:
            # Otherwise, take the number of batches as is.
            self.num_batches = num_batches
        # Initialize the exporter
        self._payload["num_batches"] = self.num_batches
        if filter_by:
            self._payload["filter_by"] = filter_by
        self._payload["shuffle"] = shuffle
        if self.kafka_address_producer:
            self._payload["kafka_address"] = self.kafka_address_producer
        # kafka_topic will be filled in later.
        # Output
        # Install query
        self.query_name = self._install_query()

    def _install_query(self):
        # Install the right GSQL query for the loader.
        e_attr_names = self.attributes
        query_replace = {"{QUERYSUFFIX}": "_".join(e_attr_names)}
        attr_types = next(iter(self._e_schema.values()))
        if e_attr_names:
            query_print = '+","+'.join(
                "{}(e.{})".format(_udf_funcs[attr_types[attr]], attr)
                for attr in e_attr_names
            )
            query_replace["{EDGEATTRS}"] = query_print
        else:
            query_replace['+ "," + {EDGEATTRS}'] = ""
        if self.kafka_address_producer:
            query_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "gsql",
                "dataloaders",
                "edge_kloader.gsql",
            )
        else:
            query_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "gsql",
                "dataloaders",
                "edge_hloader.gsql",
            )
        return install_query_file(self._graph, query_path, query_replace)

    def _start(self) -> None:
        # Create task and result queues
        self._read_task_q = Queue(self.buffer_size * 2)
        self._data_q = Queue(self.buffer_size)
        self._exit_event = Event()

        # Start requesting thread.
        if self.kafka_address_consumer:
            # If using kafka
            self._kafka_topic = "{}_{}".format(self.loader_id, self._iterations)
            self._payload["kafka_topic"] = self._kafka_topic
            self._requester = Thread(
                target=self._request_kafka,
                args=(
                    self._exit_event,
                    self._graph,
                    self.query_name,
                    self._kafka_consumer,
                    self._kafka_admin,
                    self._kafka_topic,
                    self.kafka_partitions,
                    self.kafka_replica,
                    self.max_kafka_msg_size,
                    self.kafka_retention_ms,
                    self.timeout,
                    self._payload,
                ),
            )
        else:
            # Otherwise, use rest api
            self._requester = Thread(
                target=self._request_rest,
                args=(
                    self._graph,
                    self.query_name,
                    self._read_task_q,
                    self.timeout,
                    self._payload,
                    "edge",
                ),
            )
        self._requester.start()

        # If using Kafka, start downloading thread.
        if self.kafka_address_consumer:
            self._downloader = Thread(
                target=self._download_from_kafka,
                args=(
                    self._exit_event,
                    self._read_task_q,
                    self.num_batches,
                    False,
                    self._kafka_consumer,
                ),
            )
            self._downloader.start()

        # Start reading thread.
        e_attr_types = next(iter(self._e_schema.values()))
        if self.kafka_address_consumer:
            raw_format = "edge_bytes"
        else:
            raw_format = "edge_str"
        self._reader = Thread(
            target=self._read_data,
            args=(
                self._exit_event,
                self._read_task_q,
                self._data_q,
                raw_format,
                self.output_format,
                [], [], [], {},
                self.attributes,
                [],[],
                e_attr_types
            ),
        )
        self._reader.start()

    @property
    def data(self) -> Any:
        """A property of the instance. 
        The `data` property stores all edges if all data is loaded in a single batch.
        If there are multiple batches of data, the `data` property returns the instance itself. """
        return super().data


class VertexLoader(BaseLoader):
    """Vertex Loader.
    
    Data loader that loads all vertices from the graph in batches.

    A vertex loader instance is an iterable. 
    When you loop through a vertex loader instance, it loads one batch of data from the graph to which you established a connection in each iteration.
    The size and total number of batches are specified when you define the vertex loader instance. 
    
    The boolean attribute provided to `filter_by` indicates which vertices are included.
    If you need random batches, set `shuffle` to True.

    Examples:
    The following for loop loads all vertices in the graph and prints one from each batch:

    [tabs]
    ====
    Input::
    +
    --
    [.wrap,python]
    ----
    edge_loader = conn.gds.edgeLoader(
        num_batches=10,
        attributes=["time", "is_train"],
        shuffle=True,
        filter_by=None
    )

    for i, batch in enumerate(edge_loader):
        print("----Batch {}: Shape {}----".format(i, batch.shape))
        print(batch.head(1)) <1>
    ----
    <1> Since the example does not provide an output format, the output format defaults to panda frames, have access to the methods of panda frame instances. 
    --
    Output::
    +
    --
    [.wrap,python]
    ----
    ----Batch 0: Shape (1129, 4)----
    source    target  time  is_train
    0  3145728  22020185     0         1
    ----Batch 1: Shape (1002, 4)----
        source    target  time  is_train
    0  1048577  20971586     0         1
    ----Batch 2: Shape (1124, 4)----
    source   target  time  is_train
    0       4  9437199     0         1
    ----Batch 3: Shape (1071, 4)----
        source    target  time  is_train
    0  11534340  32505859     0         1
    ----Batch 4: Shape (978, 4)----
        source    target  time  is_train
    0  11534341  16777293     0         1
    ----Batch 5: Shape (1149, 4)----
        source   target  time  is_train
    0  5242882  2097158     0         1
    ----Batch 6: Shape (1013, 4)----
        source    target  time  is_train
    0  4194305  23068698     0         1
    ----Batch 7: Shape (1037, 4)----
        source   target  time  is_train
    0  7340035  4194337     0         0
    ----Batch 8: Shape (1067, 4)----
    source   target  time  is_train
    0       3  1048595     0         1
    ----Batch 9: Shape (986, 4)----
        source    target  time  is_train
    0  9437185  13631508     0         1
    ----
    --
    ====



    See https://github.com/TigerGraph-DevLabs/mlworkbench-docs/blob/1.0/tutorials/basics/3_vertexloader.ipynb[the ML Workbench tutorial notebook]
        for more examples.
    """
    def __init__(
        self,
        graph: "TigerGraphConnection",
        attributes: Union[list, dict] = None,
        batch_size: int = None,
        num_batches: int = 1,
        shuffle: bool = False,
        filter_by: str = None,
        output_format: str = "dataframe",
        loader_id: str = None,
        buffer_size: int = 4,
        kafka_address: str = None,
        kafka_max_msg_size: int = 104857600,
        kafka_num_partitions: int = 1,
        kafka_replica_factor: int = 1,
        kafka_retention_ms: int = 60000,
        kafka_auto_del_topic: bool = True,
        kafka_address_consumer: str = None,
        kafka_address_producer: str = None,
        timeout: int = 300000,
    ) -> None:
        """
        NO DOC
        """
        super().__init__(
            graph,
            loader_id,
            num_batches,
            buffer_size,
            output_format,
            kafka_address,
            kafka_max_msg_size,
            kafka_num_partitions,
            kafka_replica_factor,
            kafka_retention_ms,
            kafka_auto_del_topic,
            kafka_address_consumer,
            kafka_address_producer,
            timeout,
        )
        # Resolve attributes
        self.attributes = self._validate_vertex_attributes(attributes)
        # Initialize parameters for the query
        self._payload = {}
        if batch_size:
            # If batch_size is given, calculate the number of batches
            num_vertices_by_type = self._graph.getVertexCount("*")
            if filter_by:
                num_vertices = sum(
                    self._graph.getVertexCount(k, where="{}!=0".format(filter_by))
                    for k in num_vertices_by_type
                )
            else:
                num_vertices = sum(num_vertices_by_type.values())
            self.num_batches = math.ceil(num_vertices / batch_size)
        else:
            # Otherwise, take the number of batches as is.
            self.num_batches = num_batches
        self._payload["num_batches"] = self.num_batches
        if filter_by:
            self._payload["filter_by"] = filter_by
        self._payload["shuffle"] = shuffle
        if self.kafka_address_producer:
            self._payload["kafka_address"] = self.kafka_address_producer
        # kafka_topic will be filled in later.
        # Install query
        self.query_name = self._install_query()

    def _install_query(self) -> str:
        # Install the right GSQL query for the loader.
        v_attr_names = self.attributes
        query_replace = {"{QUERYSUFFIX}": "_".join(v_attr_names)}
        attr_types = next(iter(self._v_schema.values()))
        if v_attr_names:
            query_print = '+","+'.join(
                "{}(s.{})".format(_udf_funcs[attr_types[attr]], attr)
                for attr in v_attr_names
            )
            query_replace["{VERTEXATTRS}"] = query_print
        else:
            query_replace['+ "," + {VERTEXATTRS}'] = ""
        if self.kafka_address_producer:
            query_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "gsql",
                "dataloaders",
                "vertex_kloader.gsql",
            )
        else:
            query_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "gsql",
                "dataloaders",
                "vertex_hloader.gsql",
            )
        return install_query_file(self._graph, query_path, query_replace)

    def _start(self) -> None:
        # Create task and result queues
        self._read_task_q = Queue(self.buffer_size * 2)
        self._data_q = Queue(self.buffer_size)
        self._exit_event = Event()

        # Start requesting thread.
        if self.kafka_address_consumer:
            # If using kafka
            self._kafka_topic = "{}_{}".format(self.loader_id, self._iterations)
            self._payload["kafka_topic"] = self._kafka_topic
            self._requester = Thread(
                target=self._request_kafka,
                args=(
                    self._exit_event,
                    self._graph,
                    self.query_name,
                    self._kafka_consumer,
                    self._kafka_admin,
                    self._kafka_topic,
                    self.kafka_partitions,
                    self.kafka_replica,
                    self.max_kafka_msg_size,
                    self.kafka_retention_ms,
                    self.timeout,
                    self._payload,
                ),
            )
        else:
            # Otherwise, use rest api
            self._requester = Thread(
                target=self._request_rest,
                args=(
                    self._graph,
                    self.query_name,
                    self._read_task_q,
                    self.timeout,
                    self._payload,
                    "vertex",
                ),
            )
        self._requester.start()

        # If using Kafka, start downloading thread.
        if self.kafka_address_consumer:
            self._downloader = Thread(
                target=self._download_from_kafka,
                args=(
                    self._exit_event,
                    self._read_task_q,
                    self.num_batches,
                    False,
                    self._kafka_consumer,
                ),
            )
            self._downloader.start()

        # Start reading thread.
        v_attr_types = next(iter(self._v_schema.values()))
        if self.kafka_address_consumer:
            raw_format = "vertex_bytes"
        else:
            raw_format = "vertex_str"
        self._reader = Thread(
            target=self._read_data,
            args=(
                self._exit_event,
                self._read_task_q,
                self._data_q,
                raw_format,
                self.output_format,
                self.attributes,
                [],
                [],
                v_attr_types,
                [],
                [],
                [],
                {},
            ),
        )
        self._reader.start()
    
    @property
    def data(self) -> Any:
        """A property of the instance. 
        The `data` property stores all data if all data is loaded in a single batch.
        If there are multiple batches of data, the `data` property returns the instance itself."""
        return super().data


class GraphLoader(BaseLoader):
    """Graph Loader.
    
    Data loader that loads all edges from the graph in batches, along with the vertices that are connected with each edge.

    Different from NeighborLoader which produces connected subgraphs, this loader
        loads all edges by batches and vertices attached to those edges.

    There are two ways to use the data loader:

    * It can be used as an iterable, which means you can loop through
          it to get every batch of data. If you load all data at once (`num_batches=1`),
          there will be only one batch (of all the data) in the iterator.
    * You can access the `data` property of the class directly. If there is
          only one batch of data to load, it will give you the batch directly instead
          of an iterator, which might make more sense in that case. If there are
          multiple batches of data to load, it will return the loader itself.

    Examples:
    The following for loop prints all edges and their connected vertices in batches.
    The output format is `PyG`:


    [tabs]
    ====
    Input::
    +
    --
    [.wrap,python]
    ----
    graph_loader = conn.gds.graphLoader(
        num_batches=10,
        v_in_feats = ["x"],
        v_out_labels = ["y"],
        v_extra_feats = ["train_mask", "val_mask", "test_mask"],
        e_in_feats=["time"],
        e_out_labels=[],
        e_extra_feats=["is_train", "is_val"],
        output_format = "PyG",
        shuffle=True,
        filter_by=None
    ) 
    for i, batch in enumerate(graph_loader):
        print("----Batch {}----".format(i))
        print(batch)
    ----
    --
    Output::
    +
    --
    ----
    ----Batch 0----
    Data(edge_index=[2, 1128], edge_feat=[1128], is_train=[1128], is_val=[1128], x=[1061, 1433], y=[1061], train_mask=[1061], val_mask=[1061], test_mask=[1061])
    ----Batch 1----
    Data(edge_index=[2, 997], edge_feat=[997], is_train=[997], is_val=[997], x=[1207, 1433], y=[1207], train_mask=[1207], val_mask=[1207], test_mask=[1207])
    ----Batch 2----
    Data(edge_index=[2, 1040], edge_feat=[1040], is_train=[1040], is_val=[1040], x=[1218, 1433], y=[1218], train_mask=[1218], val_mask=[1218], test_mask=[1218])
    ----Batch 3----
    Data(edge_index=[2, 1071], edge_feat=[1071], is_train=[1071], is_val=[1071], x=[1261, 1433], y=[1261], train_mask=[1261], val_mask=[1261], test_mask=[1261])
    ----Batch 4----
    Data(edge_index=[2, 1091], edge_feat=[1091], is_train=[1091], is_val=[1091], x=[1163, 1433], y=[1163], train_mask=[1163], val_mask=[1163], test_mask=[1163])
    ----Batch 5----
    Data(edge_index=[2, 1076], edge_feat=[1076], is_train=[1076], is_val=[1076], x=[1018, 1433], y=[1018], train_mask=[1018], val_mask=[1018], test_mask=[1018])
    ----Batch 6----
    Data(edge_index=[2, 1054], edge_feat=[1054], is_train=[1054], is_val=[1054], x=[1249, 1433], y=[1249], train_mask=[1249], val_mask=[1249], test_mask=[1249])
    ----Batch 7----
    Data(edge_index=[2, 1006], edge_feat=[1006], is_train=[1006], is_val=[1006], x=[1185, 1433], y=[1185], train_mask=[1185], val_mask=[1185], test_mask=[1185])
    ----Batch 8----
    Data(edge_index=[2, 1061], edge_feat=[1061], is_train=[1061], is_val=[1061], x=[1250, 1433], y=[1250], train_mask=[1250], val_mask=[1250], test_mask=[1250])
    ----Batch 9----
    Data(edge_index=[2, 1032], edge_feat=[1032], is_train=[1032], is_val=[1032], x=[1125, 1433], y=[1125], train_mask=[1125], val_mask=[1125], test_mask=[1125])
    ----
    --
    ====


    See https://github.com/TigerGraph-DevLabs/mlworkbench-docs/blob/1.0/tutorials/basics/3_graphloader.ipynb[the ML Workbench tutorial notebook for graph loaders]
         for examples.
    """
    def __init__(
        self,
        graph: "TigerGraphConnection",
        v_in_feats: Union[list, dict] = None,
        v_out_labels: Union[list, dict] = None,
        v_extra_feats: Union[list, dict] = None,
        e_in_feats: Union[list, dict] = None,
        e_out_labels: Union[list, dict] = None,
        e_extra_feats: Union[list, dict] = None,
        batch_size: int = None,
        num_batches: int = 1,
        shuffle: bool = False,
        filter_by: str = None,
        output_format: str = "PyG",
        add_self_loop: bool = False,
        loader_id: str = None,
        buffer_size: int = 4,
        kafka_address: str = None,
        kafka_max_msg_size: int = 104857600,
        kafka_num_partitions: int = 1,
        kafka_replica_factor: int = 1,
        kafka_retention_ms: int = 60000,
        kafka_auto_del_topic: bool = True,
        kafka_address_consumer: str = None,
        kafka_address_producer: str = None,
        timeout: int = 300000,
    ) -> None:
        """
        NO DOC
        """
        super().__init__(
            graph,
            loader_id,
            num_batches,
            buffer_size,
            output_format,
            kafka_address,
            kafka_max_msg_size,
            kafka_num_partitions,
            kafka_replica_factor,
            kafka_retention_ms,
            kafka_auto_del_topic,
            kafka_address_consumer,
            kafka_address_producer,
            timeout,
        )
        # Resolve attributes
        self.v_in_feats = self._validate_vertex_attributes(v_in_feats)
        self.v_out_labels = self._validate_vertex_attributes(v_out_labels)
        self.v_extra_feats = self._validate_vertex_attributes(v_extra_feats)
        self.e_in_feats = self._validate_edge_attributes(e_in_feats)
        self.e_out_labels = self._validate_edge_attributes(e_out_labels)
        self.e_extra_feats = self._validate_edge_attributes(e_extra_feats)
        # Initialize parameters for the query
        self._payload = {}
        if batch_size:
            # If batch_size is given, calculate the number of batches
            num_edges_by_type = self._graph.getEdgeCount("*")
            if filter_by:
                # TODO: get edge count with filter
                raise NotImplementedError
            else:
                num_edges = sum(num_edges_by_type.values())
            self.num_batches = math.ceil(num_edges / batch_size)
        else:
            # Otherwise, take the number of batches as is.
            self.num_batches = num_batches
        self._payload["num_batches"] = self.num_batches
        if filter_by:
            self._payload["filter_by"] = filter_by
        self._payload["shuffle"] = shuffle
        if self.kafka_address_producer:
            self._payload["kafka_address"] = self.kafka_address_producer
        # kafka_topic will be filled in later.
        # Output
        self.add_self_loop = add_self_loop
        # Install query
        self.query_name = self._install_query()

    def _install_query(self) -> str:
        # Install the right GSQL query for the loader.
        v_attr_names = self.v_in_feats + self.v_out_labels + self.v_extra_feats
        e_attr_names = self.e_in_feats + self.e_out_labels + self.e_extra_feats
        query_replace = {"{QUERYSUFFIX}": "_".join(v_attr_names+e_attr_names)}
        v_attr_types = next(iter(self._v_schema.values()))
        e_attr_types = next(iter(self._e_schema.values()))
        if v_attr_names:
            query_print = '+","+'.join(
                "{}(s.{})".format(_udf_funcs[v_attr_types[attr]], attr)
                for attr in v_attr_names
            )
            query_replace["{VERTEXATTRS}"] = query_print
        else:
            query_replace['+ "," + {VERTEXATTRS}'] = ""
        if e_attr_names:
            query_print = '+","+'.join(
                "{}(e.{})".format(_udf_funcs[e_attr_types[attr]], attr)
                for attr in e_attr_names
            )
            query_replace["{EDGEATTRS}"] = query_print
        else:
            query_replace['+ "," + {EDGEATTRS}'] = ""
        if self.kafka_address_producer:
            query_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "gsql",
                "dataloaders",
                "graph_kloader.gsql",
            )
        else:
            query_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "gsql",
                "dataloaders",
                "graph_hloader.gsql",
            )
        return install_query_file(self._graph, query_path, query_replace)

    def _start(self) -> None:
        # Create task and result queues
        self._read_task_q = Queue(self.buffer_size * 2)
        self._data_q = Queue(self.buffer_size)
        self._exit_event = Event()

        # Start requesting thread.
        if self.kafka_address_consumer:
            # If using kafka
            self._kafka_topic = "{}_{}".format(self.loader_id, self._iterations)
            self._payload["kafka_topic"] = self._kafka_topic
            self._requester = Thread(
                target=self._request_kafka,
                args=(
                    self._exit_event,
                    self._graph,
                    self.query_name,
                    self._kafka_consumer,
                    self._kafka_admin,
                    self._kafka_topic,
                    self.kafka_partitions,
                    self.kafka_replica,
                    self.max_kafka_msg_size,
                    self.kafka_retention_ms,
                    self.timeout,
                    self._payload,
                ),
            )
        else:
            # Otherwise, use rest api
            self._requester = Thread(
                target=self._request_rest,
                args=(
                    self._graph,
                    self.query_name,
                    self._read_task_q,
                    self.timeout,
                    self._payload,
                    "both",
                ),
            )
        self._requester.start()

        # If using Kafka, start downloading thread.
        if self.kafka_address_consumer:
            self._downloader = Thread(
                target=self._download_from_kafka,
                args=(
                    self._exit_event,
                    self._read_task_q,
                    self.num_batches,
                    True,
                    self._kafka_consumer,
                ),
            )
            self._downloader.start()

        # Start reading thread.
        v_attr_types = next(iter(self._v_schema.values()))
        e_attr_types = next(iter(self._e_schema.values()))
        if self.kafka_address_consumer:
            raw_format = "graph_bytes"
        else:
            raw_format = "graph_str"
        self._reader = Thread(
            target=self._read_data,
            args=(
                self._exit_event,
                self._read_task_q,
                self._data_q,
                raw_format,
                self.output_format,
                self.v_in_feats,
                self.v_out_labels,
                self.v_extra_feats,
                v_attr_types,
                self.e_in_feats,
                self.e_out_labels,
                self.e_extra_feats,
                e_attr_types,
                self.add_self_loop,
                True,
            ),
        )
        self._reader.start()

    @property
    def data(self) -> Any:
        """A property of the instance. 
        The `data` property stores all data if all data is loaded in a single batch.
        If there are multiple batches of data, the `data` property returns the instance itself"""
        return super().data
