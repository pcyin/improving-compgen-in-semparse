from typing import Dict, List, Tuple
import logging
import json
import glob
import os
import sqlite3
import re
from pathlib import Path
import dill

from overrides import overrides

from allennlp.common import JsonDict
from allennlp.common.file_utils import cached_path
from allennlp.common.checks import ConfigurationError
from allennlp.data.dataset_readers.dataset_reader import DatasetReader
from allennlp.data.fields import TextField, Field, SpanField, ProductionRuleField, ListField, IndexField
from allennlp.data.instance import Instance
from allennlp.data.tokenizers import Token
from allennlp.data.token_indexers import TokenIndexer, SingleIdTokenIndexer
import text2sql.data.dataset_readers.dataset_utils.text2sql_utils as local_text2sql_utils
from text2sql.semparse.worlds.text2sql_world_v3 import Text2SqlWorld
from text2sql.data.dataset_readers.dataset_utils.text2sql_utils import read_dataset_schema
from text2sql.data.tokenizers.whitespace_tokenizer import WhitespaceTokenizer

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


@DatasetReader.register("grmr_based_spans")
class GrammarBasedSpansText2SqlDatasetReader(DatasetReader):
    """
    Reads text2sql data from
    `"Improving Text to SQL Evaluation Methodology" <https://arxiv.org/abs/1806.09029>`_
    for a type constrained semantic parser.

    Parameters
    ----------
    schema_path : ``str``, required.
        The path to the database schema.
    database_path : ``str``, optional (default = None)
        The path to a database.
    use_all_sql : ``bool``, optional (default = False)
        Whether to use all of the sql queries which have identical semantics,
        or whether to just use the first one.
    remove_unneeded_aliases : ``bool``, (default = True)
        Whether or not to remove table aliases in the SQL which
        are not required.
    use_prelinked_entities : ``bool``, (default = True)
        Whether or not to use the pre-linked entities in the text2sql data.
    use_untyped_entities : ``bool``, (default = True)
        Whether or not to attempt to infer the pre-linked entity types.
    token_indexers : ``Dict[str, TokenIndexer]``, optional (default=``{"tokens": SingleIdTokenIndexer()}``)
        We use this to define the input representation for the text.  See :class:`TokenIndexer`.
        Note that the `output` tags will always correspond to single token IDs based on how they
        are pre-tokenised in the data file.
    cross_validation_split_to_exclude : ``int``, optional (default = None)
        Some of the text2sql datasets are very small, so you may need to do cross validation.
        Here, you can specify a integer corresponding to a split_{int}.json file not to include
        in the training set.
    keep_if_unparsable : ``bool``, optional (default = True)
        Whether or not to keep examples that we can't parse using the grammar.
    """
    def __init__(self,
                 schema_path: str,
                 database_file: str = None,
                 use_all_sql: bool = False,
                 use_all_queries: bool = True,
                 remove_unneeded_aliases: bool = False,
                 use_prelinked_entities: bool = True,
                 use_untyped_entities: bool = True,
                 token_indexers: Dict[str, TokenIndexer] = None,
                 cross_validation_split_to_exclude: int = None,
                 keep_if_unparsable: bool = False,
                 lazy: bool = False,
                 load_cache: bool = False,
                 save_cache: bool = True,
                 loading_limit: int = -1) -> None:
        super().__init__(lazy)
        self._token_tokenizer = WhitespaceTokenizer()
        self._token_indexers = token_indexers or {'tokens': SingleIdTokenIndexer()}
        self._use_all_sql = use_all_sql
        self._remove_unneeded_aliases = remove_unneeded_aliases
        self._use_prelinked_entities = use_prelinked_entities
        self._keep_if_unparsable = keep_if_unparsable
        self._use_all_queries = use_all_queries

        self._load_cache = load_cache
        self._save_cache = save_cache
        self._loading_limit = loading_limit

        if not self._use_prelinked_entities:
            raise ConfigurationError("The grammar based text2sql dataset reader "
                                     "currently requires the use of entity pre-linking.")

        self._cross_validation_split_to_exclude = str(cross_validation_split_to_exclude)

        if database_file:
            try:
                database_file = cached_path(database_file)
                connection = sqlite3.connect(database_file)
                self._cursor = connection.cursor()
            except FileNotFoundError as e:
                self._cursor = None
        else:
            self._cursor = None

        self._schema_path = schema_path
        self._schema = read_dataset_schema(self._schema_path)
        self._world = Text2SqlWorld(schema_path,
                                    self._cursor,
                                    use_prelinked_entities=use_prelinked_entities,
                                    use_untyped_entities=use_untyped_entities)

    @overrides
    def _read(self, file_path: str):
        """
        This dataset reader consumes the data from
        https://github.com/jkkummerfeld/text2sql-data/tree/master/data
        formatted using ``scripts/reformat_text2sql_data.py``.

        Parameters
        ----------
        file_path : ``str``, required.
            For this dataset reader, file_path can either be a path to a file `or` a
            path to a directory containing json files. The reason for this is because
            some of the text2sql datasets require cross validation, which means they are split
            up into many small files, for which you only want to exclude one.
        """
        # For example, file scholar/schema_full_split/aligned_final_dev.json will be saved in
        # scholar/schema_full_split/attnsupgrammar_cache_aligned_final_dev
        file_path = Path(file_path)
        cache_dir = os.path.join(file_path.parent, f'spansgrammar_cache_{file_path.stem}')
        if self._load_cache:
            logger.info(f'Trying to load cache from {cache_dir}')
            if not os.path.isdir(cache_dir):
                logger.info(f'Can\'t load cache, cache {cache_dir} doesn\'t exits')
                self._load_cache = False
        if self._save_cache:
            os.makedirs(cache_dir, exist_ok=True)

        files = [p for p in glob.glob(str(file_path))
                 if self._cross_validation_split_to_exclude not in os.path.basename(p)]
        cnt = 0  # used to limit the number of loaded instances
        for path in files:
            with open(cached_path(path), "r") as data_file:
                data = json.load(data_file)
            total_cnt = -1  # used to name the cache files
            for sql_data in local_text2sql_utils.process_sql_data(data,
                                                                  use_all_sql=self._use_all_sql,
                                                                  remove_unneeded_aliases=self._remove_unneeded_aliases,
                                                                  schema=self._schema,
                                                                  use_all_queries=self._use_all_queries,
                                                                  load_spans=True):
                # Handle caching - only caching instances that are not None
                # (any non parsable sql query will result in None)
                total_cnt += 1
                cache_filename = f'instance-{total_cnt}.pt'
                cache_filepath = os.path.join(cache_dir, cache_filename)
                if self._loading_limit == cnt:
                    break
                if self._load_cache:
                    try:
                        instance = dill.load(open(cache_filepath, 'rb'))
                        cnt += 1
                        yield instance
                    except Exception as e:
                        # could not load from cache - keep loading without cache
                        pass
                else:
                    linked_entities = sql_data.sql_variables if self._use_prelinked_entities else None
                    instance = self.text_to_instance(query=sql_data.text_with_variables,
                                                     derived_cols=sql_data.derived_cols,
                                                     derived_tables=sql_data.derived_tables,
                                                     prelinked_entities=linked_entities,
                                                     sql=sql_data.sql,
                                                     spans=sql_data.spans)
                    if instance is not None:
                        cnt += 1
                        if self._save_cache:
                            dill.dump(instance, open(cache_filepath, 'wb'))
                        yield instance

    @overrides
    def text_to_instance(self,  # type: ignore
                         query: List[str],
                         derived_cols: List[Tuple[str, str]],
                         derived_tables: List[str],
                         prelinked_entities: Dict[str, Dict[str, str]] = None,
                         sql: List[str] = None,
                         spans: List[Tuple[int, int]] = None) -> Instance:
        # pylint: disable=arguments-differ
        fields: Dict[str, Field] = {}
        tokens_tokenized = self._token_tokenizer.tokenize(' '.join(query))
        tokens = TextField(tokens_tokenized, self._token_indexers)
        fields["tokens"] = tokens

        spans_field: List[Field] = []
        spans = self._fix_spans_coverage(spans, len(tokens_tokenized))
        for start, end in spans:
            spans_field.append(SpanField(start, end, tokens))
        span_list_field: ListField = ListField(spans_field)
        fields["spans"] = span_list_field

        if sql is not None:
            action_sequence, all_actions = self._world.get_action_sequence_and_all_actions(query=sql,
                                                                                           derived_cols=derived_cols,
                                                                                           derived_tables=derived_tables,
                                                                                           prelinked_entities=prelinked_entities)
            if action_sequence is None and self._keep_if_unparsable:
                print("Parse error")
                action_sequence = []
            elif action_sequence is None:
                return None

        index_fields: List[Field] = []
        production_rule_fields: List[Field] = []

        for production_rule in all_actions:
            nonterminal, _ = production_rule.split(' ->')
            production_rule = ' '.join(production_rule.split(' '))
            field = ProductionRuleField(production_rule,
                                        self._world.is_global_rule(nonterminal),
                                        nonterminal=nonterminal)
            production_rule_fields.append(field)

        valid_actions_field = ListField(production_rule_fields)
        fields["valid_actions"] = valid_actions_field

        action_map = {action.rule: i # type: ignore
                      for i, action in enumerate(valid_actions_field.field_list)}

        for production_rule in action_sequence:
            index_fields.append(IndexField(action_map[production_rule], valid_actions_field))
        if not action_sequence:
            index_fields = [IndexField(-1, valid_actions_field)]
        # if not action_sequence and re.findall(r"COUNT \( \* \) (?:<|>|<>|=) 0", " ".join(sql)):
        #     index_fields = [IndexField(-2, valid_actions_field)]

        action_sequence_field = ListField(index_fields)
        fields["action_sequence"] = action_sequence_field
        return Instance(fields)

    def read_json_dict(self, json_dict: JsonDict) -> Instance:
        """
        Expectied keys:
        question: string
        sql: string
        variables: a dictionary with the variables as keys, and original entities as values - {'author0': 'jane doe'}
        """
        text_vars_str = json_dict['variables'].replace('\'','"')
        text_vars = json.loads(text_vars_str)
        sql_vars = [{'name': k, 'example': v, 'type': k[:-1]} for k,v in text_vars.items()]
        data = [{'sentences': [{'text': json_dict['question'], 'question-split': 'question', 'variables': text_vars}],
                'sql': [json_dict['sql']],
                'variables': sql_vars}]

        for sql_data in local_text2sql_utils.process_sql_data(data,
                                                              use_all_sql=self._use_all_sql,
                                                              remove_unneeded_aliases=self._remove_unneeded_aliases,
                                                              schema=self._schema,
                                                              use_all_queries=self._use_all_queries):
            linked_entities = sql_data.sql_variables if self._use_prelinked_entities else None
            instance = self.text_to_instance(query=sql_data.text_with_variables,
                                             derived_cols=sql_data.derived_cols,
                                             derived_tables=sql_data.derived_tables,
                                             prelinked_entities=linked_entities,
                                             sql=sql_data.sql)
            return instance

    @staticmethod
    def _fix_spans_coverage(spans: List[Tuple[int, int]], source_length: int):
        """
        Given a list of spans, fix them to be inclusive and adds all the size 1 spans
        """
        # add -1 to the end indices to make inclusive
        new_spans: List[Tuple[int, int]] = []
        for s, e in spans:
            new_spans.append((s, e-1))
        spans_set = set(new_spans)
        # add all size 1 spans
        for i in range(source_length):
            spans_set.add((i, i))
        return spans_set


if __name__ == '__main__':
    for dataset in ['atis', 'advising', 'geography', 'scholar']:
        c = GrammarBasedSpansText2SqlDatasetReader(
            schema_path=f"/datainbaro2/text2sql/parsers_models/allennlp_text2sql/data/sql data/{dataset}-schema.csv",
            use_all_sql=False,
            use_all_queries=True,
            use_prelinked_entities=True,
            use_untyped_entities=True,
            keep_if_unparsable=False)
        for split_type in ['new_question_split', 'schema_full_split']:
            for split in ['aligned_train', 'aligned_final_dev']:
                if dataset == 'advising':
                    split = '_'.join(split.split('_')[:-1]) + '_new_no_join_'+ split.split('_')[-1]
                data = c.read(f'/datainbaro2/text2sql/parsers_models/allennlp_text2sql/data/sql data/{dataset}/{split_type}/{split}.json')

