# This file is part of the DiscoPoP software (http://www.discopop.tu-darmstadt.de)
#
# Copyright (c) 2020, Technische Universitaet Darmstadt, Germany
#
# This software may be modified and distributed under the terms of
# the 3-Clause BSD License.  See the LICENSE file in the package base
# directory for details.


import copy
import os
from typing import List

from cpp_demangle import demangle

from lxml import etree
from lxml import objectify

from PETGraphX import PETGraphX, NodeType, CUNode, DepType, EdgeType, MWType
from parser import parse_inputs
from pattern_detectors.PatternInfo import PatternInfo
from utils import depends, calculate_workload, \
    total_instructions_count, classify_task_vars

__forks = set()
__workloadThreshold = 10000
__minParallelism = 3


class Task(object):
    """This class represents task in task parallelism pattern
    """
    nodes: List[CUNode]
    child_tasks: List['Task']
    start_line: str
    end_line: str

    def __init__(self, pet: PETGraphX, node: CUNode):
        self.node_id = node.id
        self.nodes = [node]
        self.start_line = node.start_position()
        if ":" in self.start_line:
            self.region_start_line = self.start_line[self.start_line.index(":") + 1:]
        else:
            self.region_start_line = self.start_line
        self.region_end_line = None
        self.end_line = node.end_position()
        self.mw_type = node.mw_type
        self.instruction_count = total_instructions_count(pet, node)
        self.workload = calculate_workload(pet, node)
        self.child_tasks = []

    def aggregate(self, other: 'Task'):
        """Aggregates given task into current task

        :param other: task to aggregate
        """
        self.nodes.extend(other.nodes)
        self.end_line = other.end_line
        self.workload += other.workload
        self.instruction_count += other.instruction_count
        self.mw_type = MWType.BARRIER_WORKER if other.mw_type == MWType.BARRIER_WORKER else MWType.WORKER


def __merge_tasks(pet: PETGraphX, task: Task):
    """Merges the tasks into having required workload.

    :param pet: PET graph
    :param task: task node
    """
    for i in range(len(task.child_tasks)):
        child_task: Task = task.child_tasks[i]
        if child_task.workload < __workloadThreshold:
            if i > 0:
                pred: Task = task.child_tasks[i - 1]
                if __neighbours(pred, child_task):
                    pred.aggregate(child_task)
                    pred.child_tasks.remove(child_task)
                    __merge_tasks(pet, task)
                    return
            if i + 1 < len(task.child_tasks) - 1:
                succ: Task = task.child_tasks[i + 1]
                if __neighbours(child_task, succ):
                    child_task.aggregate(succ)
                    task.child_tasks.remove(succ)
                    __merge_tasks(pet, task)
                    return
            task.child_tasks.remove(child_task)
            __merge_tasks(pet, task)
            return

    if task.child_tasks and len(task.child_tasks) < __minParallelism:
        max_workload_task = max(task.child_tasks, key=lambda t: t.workload)
        task.child_tasks.extend(max_workload_task.child_tasks)
        task.child_tasks.remove(max_workload_task)
        __merge_tasks(pet, task)
        return

    for child in task.child_tasks:
        if child.nodes[0].type == NodeType.LOOP:
            pass


def __neighbours(first: Task, second: Task):
    """Checks if second task immediately follows first task

    :param first: predecessor task
    :param second: successor task
    :return: true if second task immediately follows first task
    """
    fel = int(first.end_line.split(':')[1])
    ssl = int(second.start_line.split(':')[1])
    return fel == ssl or fel + 1 == ssl or fel + 2 == ssl


class TaskParallelismInfo(PatternInfo):
    """Class, that contains task parallelism detection result
    """

    def __init__(self, node: CUNode, pragma, pragma_line, first_private, private, shared):
        """
        :param node: node, where task parallelism was detected
        :param pragma: pragma to be used (task / taskwait)
        :param pragma_line: line prior to which the pragma shall be inserted
        :param first_private: list of varNames
        :param private: list of varNames
        :param shared: list of varNames
        """
        PatternInfo.__init__(self, node)
        self.pragma = pragma
        self.pragma_line = pragma_line
        if ":" in self.pragma_line:
            self.region_start_line = self.pragma_line[self.pragma_line.index(":") + 1:]
        else:
            self.region_start_line = self.pragma_line
        self.region_end_line = None
        self.first_private = first_private
        self.private = private
        self.shared = shared
        self.in_dep = []
        self.out_dep = []
        self.in_out_dep = []

    def __str__(self):
        return f'Task parallelism at CU: {self.node_id}\n' \
               f'CU Start line: {self.start_line}\n' \
               f'CU End line: {self.end_line}\n' \
               f'pragma at line: {self.pragma_line}\n' \
               f'pragma region start line: {self.region_start_line}\n' \
               f'pragma region end line: {self.region_end_line}\n' \
               f'pragma: "#pragma omp {" ".join(self.pragma)}"\n' \
               f'first_private: {" ".join(self.first_private)}\n' \
               f'private: {" ".join(self.private)}\n' \
               f'shared: {" ".join(self.shared)}\n' \
               f'in_dep: {" ".join(self.in_dep)}\n' \
               f'out_dep: {" ".join(self.out_dep)}\n' \
               f'in_out_dep: {" ".join(self.in_out_dep)}\n'


class ParallelRegionInfo(PatternInfo):
    """Class, that contains parallel region info.
    """

    def __init__(self, node: CUNode,
                 region_start_line, region_end_line):
        PatternInfo.__init__(self, node)
        self.region_start_line = region_start_line
        self.region_end_line = region_end_line
        self.pragma = "#pragma omp parallel\n\t#pragma omp single"

    def __str__(self):
        return f'Task Parallel Region at CU: {self.node_id}\n' \
               f'CU Start line: {self.start_line}\n' \
               f'CU End line: {self.end_line}\n' \
               f'pragma: \n\t{self.pragma}\n' \
               f'Parallel Region Start line: {self.region_start_line}\n' \
               f'Parallel Region End line {self.region_end_line}\n'


class OmittableCuInfo(PatternInfo):
    """Class, that contains information on omittable CUs (such that can be
    combined with a suggested task).
    Objects of this type are only intermediate and will not show up in the
    final suggestions.
    """

    def __init__(self, node: CUNode, combine_with_node: CUNode):
        PatternInfo.__init__(self, node)
        self.combine_with_node = combine_with_node
        # only for printing
        self.cwn_id = combine_with_node.id
        self.in_dep = []
        self.out_dep = []
        self.in_out_dep = []

    def __str__(self):
        return f'Omittable CU: {self.node_id}\n' \
               f'CU Start line: {self.start_line}\n' \
               f'CU End line: {self.end_line}\n' \
               f'Combinable with: {self.cwn_id}\n' \
               f'in_dep: {" ".join(self.in_dep)}\n' \
               f'out_dep: {" ".join(self.out_dep)}\n' \
               f'in_out_dep: {" ".join(self.in_out_dep)}\n'


def build_preprocessed_graph_and_run_detection(cu_xml: str, dep_file: str, loop_counter_file: str, reduction_file: str,
                                               file_mapping: str):
    """execute preprocessing of given cu xml file and construct a new cu graph.
    execute run_detection on newly constructed graph afterwards.
    :param cu_xml: Path (string) to the CU xml file to be used
    :param dep_file: Path (string) to the dependence file to be used
    :param loop_counter_file: Path (string) to the loop counter file file to be used
    :param reduction_file: Path (string) to the reduction file to be used
    :return: List of detected pattern info
    """
    preprocessed_cu_xml = cu_xml_preprocessing(cu_xml)
    cu_dict, dependencies, loop_data, reduction_vars = parse_inputs(preprocessed_cu_xml, dep_file,
                                                                    loop_counter_file, reduction_file)
    preprocessed_graph = PETGraphX(cu_dict, dependencies,
                                   loop_data, reduction_vars)
    # execute reduction detector to enable taskloop-reduction-detection
    from pattern_detectors.reduction_detector import run_detection as detect_reduction
    detect_reduction(preprocessed_graph)
    suggestions = run_detection(preprocessed_graph, preprocessed_cu_xml, file_mapping, dep_file)
    return suggestions


def run_detection(pet: PETGraphX, cu_xml: str, file_mapping: str, dep_file: str) -> List[TaskParallelismInfo]:
    """Computes the Task Parallelism Pattern for a node:
    (Automatic Parallel Pattern Detection in the Algorithm Structure Design Space p.46)
    1.) first merge all children of the node -> all children nodes get the dependencies
        of their children nodes and the list of the children nodes (saved in node.childrenNodes)
    2.) To detect Task Parallelism, we use Breadth First Search (BFS)
        a.) the hotspot becomes a fork
        b.) all child nodes become first worker if they are not marked as worker before
        c.) if a child has dependence to more than one parent node, it will be marked as barrier
    3.) if two barriers can run in parallel they are marked as barrierWorkers.
        Two barriers can run in parallel if there is not a directed path from one to the other
        :param pet: PET graph
        :param cu_xml: Path (string) to the CU xml file to be used
        :return: List of detected pattern info
    """
    result = []

    for node in pet.all_nodes():

        if node.type == NodeType.DUMMY:
            continue
        if pet.direct_children(node):
            __detect_mw_types(pet, node)

        if node.mw_type == MWType.NONE:
            node.mw_type = MWType.ROOT

    __forks.clear()
    __create_task_tree(pet, pet.main)

    # ct = [graph.vp.id[v] for v in pet.graph.vp.childrenTasks[main_node]]
    # ctt = [graph.vp.id[v] for v in forks]
    fs = [f for f in __forks if f.node_id == '130:0']

    for fork in fs:
        if fork.child_tasks:
            result.append(TaskParallelismInfo(fork.nodes[0], ["dummy_fork"], [], [], [], []))
    result += __detect_task_suggestions(pet)
    result += __suggest_parallel_regions(pet, result)
    result = __set_task_contained_lines(result)
    result = __detect_taskloop_reduction(pet, result)
    result = __remove_useless_barrier_suggestions(pet, result)
    result = __detect_barrier_suggestions(pet, result)
    result = __validate_barriers(pet, result)
    result = __suggest_missing_barriers_for_global_vars(pet, result)
    # result = __detect_dependency_clauses(pet, result)
    result = __detect_dependency_clauses_alias_based(pet, result, file_mapping, dep_file)
    result = __combine_omittable_cus(pet, result)
    result = __suggest_barriers_for_uncovered_tasks_before_return(pet, result)
    result = __suggest_shared_clauses_for_all_tasks_in_function_body(pet, result)
    result = __remove_duplicates(result)
    result = __filter_data_sharing_clauses(pet, result, __get_var_definition_line_dict(cu_xml))
    result = __filter_data_depend_clauses(pet, result, __get_var_definition_line_dict(cu_xml))
    result = __remove_duplicate_data_sharing_clauses(result)
    result = __sort_output(result)

    return result



def __detect_dependency_clauses_alias_based(pet: PETGraphX, suggestions: [PatternInfo], file_mapping_path: str,
                                            dep_file: str):
    """TODO"""
    # Read contents of file_mapping
    source_code_files = dict()
    with open(file_mapping_path) as f:
        for line in f.readlines():
            line = line.replace("\n", "")
            line = line.split("\t")
            source_code_files[line[0]] = line[1]
    # get RAW depencency information as a dict
    raw_dependency_information = __get_RAW_dependency_information_from_dep_file(dep_file)
    aliases = __get_alias_information(pet, suggestions, source_code_files)
    suggestions = __identify_dependencies_for_different_functions(pet, suggestions, aliases, source_code_files,
                                                                  raw_dependency_information)
    return suggestions


def __identify_dependencies_for_different_functions(pet: PETGraphX, suggestions: [PatternInfo], aliases,
                                                    source_code_files, raw_dependency_information: dict):
    """TODO, adds dependency clauses to task suggestions"""
    # wrapper to start __check_dependence_of_task_pair for all viable combinations of suggested tasks
    result_suggestions = []
    task_suggestions = []
    for s in suggestions:
        if type(s) == TaskParallelismInfo:
            if s.pragma[0] == "task":
                task_suggestions.append(s)
            else:
                result_suggestions.append(s)
        else:
            result_suggestions.append(s)
    # iterate over all combinations of tasks, ts_1 has to come before ts_2  # TODO: possible / required to filter for tasks inside same function?
    # get in and out dependences to insert
    out_dep_updates = dict()
    in_dep_updates = dict()
    for ts_1 in task_suggestions:
        # get parent function
        for parent_function_1 in [pet.node_at(e[0]) for e in pet.in_edges(ts_1._node.id, EdgeType.CHILD)
                                  if pet.node_at(e[0]).type == NodeType.FUNC]:
            # get recursive function call from original source code
            function_call_string_1 = ""
            try:
                function_call_string_1 = __get_function_call_from_source_code(source_code_files, int(ts_1.pragma_line),
                                                                              ts_1.node_id.split(":")[0])
            except IndexError:
                continue
            # get function parameter names from recursive function call
            function_name_1, parameter_names_1 = __get_called_function_and_parameter_names_from_function_call(
                function_call_string_1, ts_1._node.recursive_function_calls[0], ts_1._node)
            for ts_2 in [s for s in task_suggestions if not s == ts_1 and int(ts_1.pragma_line) <= int(s.pragma_line)]:
                # get parent function
                for parent_function_2 in [pet.node_at(e[0]) for e in pet.in_edges(ts_2._node.id, EdgeType.CHILD)
                                          if pet.node_at(e[0]).type == NodeType.FUNC]:
                    # get recursive function call from original source code
                    function_call_string_2 = ""
                    try:
                        function_call_string_2 = __get_function_call_from_source_code(source_code_files,
                                                                                      int(ts_2.pragma_line),
                                                                                      ts_2.node_id.split(":")[0])
                    except IndexError:
                        continue
                    # get function parameter names from recursive function call
                    function_name_2, parameter_names_2 = __get_called_function_and_parameter_names_from_function_call(
                        function_call_string_2, ts_2._node.recursive_function_calls[0], ts_2._node)
                    dependencies = __check_dependence_of_task_pair(aliases, raw_dependency_information,
                                                                  ts_1, function_call_string_1, parameter_names_1,
                                                                  ts_2, function_call_string_2, parameter_names_2)
                    for dependence_var in dependencies:
                        # Mark the variable as depend out for the first function and depend in for the second function.
                        if not ts_1 in out_dep_updates:
                            out_dep_updates[ts_1] = []
                        out_dep_updates[ts_1].append(dependence_var)
                        if not ts_2 in in_dep_updates:
                            in_dep_updates[ts_2] = []
                        in_dep_updates[ts_2].append(dependence_var)
    # perform updates of in and out dependencies
    for ts in task_suggestions:
        if ts in out_dep_updates:
            for out_dep_var in out_dep_updates[ts]:
                ts.out_dep.append(out_dep_var)
            ts.out_dep = list(set(ts.out_dep))
        if ts in in_dep_updates:
            for in_dep_var in in_dep_updates[ts]:
                ts.in_dep.append(in_dep_var)
            ts.in_dep = list(set(ts.in_dep))
        result_suggestions.append(ts)

    return result_suggestions


def __check_dependence_of_task_pair(aliases, raw_dependency_information: dict,
                                    task_suggestion_1: TaskParallelismInfo, call_string_1: str, param_names_1: [str],
                                    task_suggestion_2: TaskParallelismInfo, call_string_2: str, param_names_2: [str]):
    """TODO"""
    dependences = []
    # iterate over parameters of task_1
    for parameter in param_names_1:
        if parameter is None:
            continue
        # get aliases for parameter
        for alias_entry in aliases[task_suggestion_1]:
            # skip wrong alias entries
            if not alias_entry[0][0] == parameter:
                continue
            # intersect alias_entry of task_suggestion_1 with entries of task_suggestion_2
            alias_entries_2 = []
            for alias_entry_2 in aliases[task_suggestion_2]:
                alias_entries_2 += alias_entry_2
            intersection = list(set([ae for ae in alias_entry if ae in alias_entries_2]))
            # get sink lines
            # (start and end line of task_sug_1's parent func)
            sink_lines_start = alias_entry[0][2].split(":")
            sink_lines_end = alias_entry[0][3].split(":")
            sink_lines = []
            for ln in range(int(sink_lines_start[1]), int(sink_lines_end[1]) + 1):
                sink_lines.append("" + sink_lines_start[0] + ":" + str(ln))
            # check if there is a RAW on the variable within sink lines and source lines.
            for intersection_entry in intersection:
                # get intersection variable and source lines
                intersecting_variable = intersection_entry[0]
                source_lines_start = intersection_entry[2].split(":")
                source_lines_end = intersection_entry[3].split(":")
                source_lines = []
                for ln in range(int(source_lines_start[1]), int(source_lines_end[1])+1):
                    source_lines.append(""+source_lines_start[0]+":"+str(ln))
                # check if there is a RAW on the variable within sink lines and source lines.
                if source_lines == sink_lines:
                    continue
                for source_line in source_lines:
                    if not source_line in raw_dependency_information:
                        continue
                    for raw_dep_entry in raw_dependency_information[source_line]:
                        if raw_dep_entry[1] == intersecting_variable:
                            if raw_dep_entry[0] in sink_lines:
                                print("DEPENDENCE: ", parameter)
                                dependences.append(parameter)

    dependences = list(set(dependences))
    return dependences


def __get_RAW_dependency_information_from_dep_file(dep_file) -> dict:
    """TODO
    Format: {source_line: [(sink_line, var_name)]"""
    raw_dependencies = dict()
    with open(dep_file) as f:
        for line in f.readlines():
            line = line.replace("\n", "")
            # format of dependency entries in _dep.txt-file:
            #   sourceLine NOM RAW sinkLine|variable
            if not " NOM " in line:
                continue
            split_line = line.split(" NOM ")
            source_line = split_line[0]
            # split entries
            entries = []
            current_entry = ""
            for word in split_line[1].split(" "):
                word = word.replace(" ", "")
                if word == "RAW" or word == "WAR" or word == "WAW" or word == "INIT":
                    if len(current_entry) > 0:
                        entries.append(current_entry)
                    current_entry = ""
                if len(current_entry) > 0:
                    current_entry += " "+word
                else:
                    current_entry += word
            if len(current_entry) > 0:
                entries.append(current_entry)
            if source_line not in raw_dependencies:
                raw_dependencies[source_line] = []
            for entry in entries:
                # filter for RAW dependencies
                split_entry = entry.split(" ")
                if split_entry[0] != "RAW":
                    continue
                split_sink_line_var = split_entry[1].split("|")
                sink_line = split_sink_line_var[0]
                var_name = split_sink_line_var[1].replace(".addr", "")
                raw_dependencies[source_line].append((sink_line, var_name))
    return raw_dependencies


def __get_alias_information(pet: PETGraphX, suggestions: [PatternInfo], source_code_files: dict):
    """TODO"""
    # iterate over task suggestions
    task_suggestions = [s for s in [e for e in suggestions if type(e) == TaskParallelismInfo] if s.pragma[0] == "task"]
    # collect alias information
    aliases = dict()
    for ts in task_suggestions:
        current_alias_entry = []
        # get parent function
        for parent_function in [pet.node_at(e[0]) for e in pet.in_edges(ts._node.id, EdgeType.CHILD)
                                if pet.node_at(e[0]).type == NodeType.FUNC]:
            # get recursive function call from original source code
            function_call_string = ""
            try:
                function_call_string = __get_function_call_from_source_code(source_code_files, int(ts.pragma_line),
                                                                            ts.node_id.split(":")[0])
            except IndexError:
                continue
            # get function parameter names from recursive function call
            function_name, parameter_names = __get_called_function_and_parameter_names_from_function_call(function_call_string,
                ts._node.recursive_function_calls[0], ts._node)
            # get CU Node object of called function
            called_function_cu_id = None
            for recursive_function_call_entry in ts._node.recursive_function_calls:
                if "," in recursive_function_call_entry:
                    recursive_function_call_entry = recursive_function_call_entry.split(",")[0]
                recursive_function_call_entry = recursive_function_call_entry.split(" ")
                recursive_function_call_line = recursive_function_call_entry[1]
                if int(recursive_function_call_line.split(":")[1]) == int(ts.pragma_line):
                    # correct function call found
                    # find corresponding function CU
                    for tmp_func_cu in pet.all_nodes(NodeType.FUNC):
                        if tmp_func_cu.name == recursive_function_call_entry[0]:
                            called_function_cu_id = tmp_func_cu.id
            # get aliases for parameters
            for idx, param in enumerate(parameter_names):
                if param is None:
                    continue
                current_alias = [(param, parent_function.name, parent_function.start_position(),
                                  parent_function.end_position())]
                current_alias += __get_alias_for_parameter_at_position(pet, pet.node_at(called_function_cu_id), idx,
                                                                       source_code_files, [])
                current_alias_entry.append(current_alias)
        aliases[ts] = current_alias_entry
    # join aliases on first element (original identifier)
    for key in aliases:
        joined_aliases = []
        while aliases[key]:
            join_on = aliases[key].pop()
            join_indices = []
            for idx, alias_entry in enumerate(aliases[key]):
                if alias_entry[0] == join_on[0]:
                    join_indices.append(idx)
            # sort reversed to prevent errors due to popping elements
            join_indices.sort(reverse=True)
            for idx in join_indices:
                to_be_joined = aliases[key].pop(idx)
                to_be_joined.pop(0)
                join_on += to_be_joined
            joined_aliases.append(join_on)
        aliases[key] = joined_aliases
    return aliases


def __get_alias_for_parameter_at_position(pet: PETGraphX, function: CUNode, parameter_position: int, source_code_files, visited):
    """TODO"""
    visited.append((function, parameter_position))
    parameter_name = function.args[parameter_position].name
    # append Alias information for parameter to result
    result = [(parameter_name, function.name, function.start_position(), function.end_position())]

    # find function calls which use the parameter
        # iterate over CUs
    for cu in [pet.node_at(cuid) for cuid in [e[1] for e in pet.out_edges(function.id)]]:
        # iterate over children of CU and retrieve called functions
        called_functions = []
        for child in [pet.node_at(cuid) for cuid in [e[1] for e in pet.out_edges(cu.id)]]:
            # check if type is Func or Dummy
            if child.type == NodeType.FUNC or child.type == NodeType.DUMMY:
                # CU contains a function call
                # if Dummy, map to Func
                if child.type == NodeType.DUMMY:
                    for function_cu in pet.all_nodes(NodeType.FUNC):
                        if child.name == function_cu.name:
                            child = function_cu
                called_functions.append(child)
        # iterate over called functions
        for called_function in called_functions:
            # read line from source code (iterate over lines of CU to search for function call)
            for line in range(cu.start_line, cu.end_line+1):
                source_code_line = ""
                try:
                    source_code_line = __get_function_call_from_source_code(source_code_files, line, cu.id.split(":")[0])
                except IndexError:
                    continue
                # get parameter names from call

                function_name, call_parameters = __get_called_function_and_parameter_names_from_function_call(source_code_line, called_function.name, cu)
                # check if parameter_name is contained
                for idx, pn in enumerate(call_parameters):
                    if pn == parameter_name:
                        # check if same configuration for alias detection has been used:
                        if (called_function, idx) not in visited:
                            # if not, start recursion
                            result += __get_alias_for_parameter_at_position(pet, called_function, idx, source_code_files, visited)
    return result


def __get_function_call_from_source_code(source_code_files, line_number, file_id):
    """TODO"""
    source_code = open(source_code_files[file_id])
    source_code_lines = source_code.readlines()
    offset = -1
    function_call_string = source_code_lines[line_number + offset]
    while function_call_string.count("(") > function_call_string.count(")") or function_call_string.count("(") < 1:
        offset += 1
        function_call_string += source_code_lines[line_number + offset]
    function_call_string = function_call_string.replace("\n", "")
    return function_call_string


def __get_called_function_and_parameter_names_from_function_call(source_code_line: str, mangled_function_name: str,
                                                                 node: CUNode):
    """TODO
    If parameter is a complex expression (e.g. addition, or function call, None is used at the respective position.
    Returns None if function name not in source_code_line"""
    # find function name by finding biggest match between function call line and recursive call
    mangled_function_name = mangled_function_name.split(" ")[0]  # ignore line if present
    function_name = demangle(mangled_function_name).split("(")[0]
    if not function_name in source_code_line:
        return (None, [])

    # get parameters in brackets
    # parameter_string = source_code_line[function_position:]
    parameter_string = source_code_line[source_code_line.find(function_name) + len(function_name):]
        # prune left
    while not parameter_string.startswith(("(")):
        parameter_string = parameter_string[parameter_string.find("("):]
        # prune right
    while not parameter_string.endswith((")")):
        parameter_string = parameter_string[:parameter_string.rfind(")")+1]
        # prune to correct amount of closing brackets
    while not parameter_string.count("(") == parameter_string.count(")"):
        parameter_string = parameter_string[:-1]
        parameter_string = parameter_string[:parameter_string.rfind(")")+1]
    parameter_string = parameter_string[1:-1]
    # intersect parameters with set of known variables to prevent errors
    parameters = parameter_string.split(",")
    result_parameters = []
    for param in parameters:
        if "+" in param or "-" in param or "*" in param or "/" in param or "(" in param or ")" in param:
            result_parameters.append(None)
        else:
            # check if param in known variables:
            if param.replace(" ", "") in [var.replace(".addr", "") for var in [v.name for v in node.local_vars+node.global_vars]]:
                result_parameters.append(param.replace(" ", ""))
    return function_name, result_parameters


def __suggest_shared_clauses_for_all_tasks_in_function_body(pet: PETGraphX, suggestions: [PatternInfo]):
    """Marks unmentioned variables as shared, if they occur as shared in a different task suggestions
    inside the parent functions body.
    :param: pet: PET graph
    :param: suggestions: List[PatternInfo]
    :return: List[PatternInfo]
    """
    task_suggestions = [s for s in suggestions if s.pragma[0] == "task"]
    for ts in task_suggestions:
        if ts.shared:
            # iterate over parent function(s)
            for parent_function in [pet.node_at(e[0]) for e in pet.in_edges(ts._node.id, EdgeType.CHILD)
                                    if pet.node_at(e[0]).type == NodeType.FUNC]:
                # get task suggestions in parent functions scope
                for other_suggestion in task_suggestions:
                    if not (__line_contained_in_region(other_suggestion.start_line, parent_function.start_position(),
                                                       parent_function.end_position())
                            and
                            __line_contained_in_region(other_suggestion.end_line, parent_function.start_position(),
                                                       parent_function.end_position())):
                        # other suggestion not part of parent function
                        continue
                    # mark shared variables of ts as shared in other_suggestion, if not already mentioned
                    for var in ts.shared:
                        if var in other_suggestion.shared:
                            continue
                        if var in other_suggestion.private:
                            continue
                        if var in other_suggestion.first_private:
                            continue
                        other_suggestion.shared.append(var)

    return suggestions


def __remove_duplicate_data_sharing_clauses(suggestions: [PatternInfo]):
    """removes duplicates from in, out and in-out dependency lists.
    Mainly needed for printing purposes.
    :param suggestions: List[PatternInfo]
    :return: Modified List of PatternInfos
    """
    result = []
    for s in suggestions:
        if not type(s) == TaskParallelismInfo:
            result.append(s)
        else:
            s.in_dep = list(set(s.in_dep))
            s.out_dep = list(set(s.out_dep))
            s.in_out_dep = list(set(s.in_out_dep))
    return suggestions


def __get_var_definition_line_dict(cu_xml: str):
    """creates a dictionary {varname: [definitionLines]} based on cu_xml
    and return the dictionary.
    Removes .addr suffix if present.
    :param cu_xml: Path (string) to the CU xml file to be used
    :return: dictionary, containing information on variable definition lines
    """
    xml_fd = open(cu_xml)
    xml_content = ""
    for line in xml_fd.readlines():
        if not (line.rstrip().endswith('</Nodes>') or line.rstrip().endswith('<Nodes>')):
            xml_content = xml_content + line
    xml_content = "<Nodes>{0}</Nodes>".format(xml_content)
    parsed_cu = objectify.fromstring(xml_content)

    var_def_line_dict = dict()
    for node in parsed_cu.Node:
        # only consider cu nodes
        if node.get("type") == "0":
            # add global variables
            for idx, global_variables_entry in enumerate(node.globalVariables):
                try:
                    for i in global_variables_entry["global"]:
                        # insert mapping into var_def_line_dict
                        if not i.text.replace(".addr", "") in var_def_line_dict:
                            var_def_line_dict[i.text.replace(".addr", "")] = [i.get("defLine")]
                        else:
                            var_def_line_dict[i.text.replace(".addr", "")].append(i.get("defLine"))
                            var_def_line_dict[i.text.replace(".addr", "")] = list(
                                set(var_def_line_dict[i.text.replace(".addr", "")]))
                except AttributeError:
                    pass
            # add local variables
            for idx, global_variables_entry in enumerate(node.localVariables):
                try:
                    for i in global_variables_entry["local"]:
                        # insert mapping into var_def_line_dict
                        if not i.text.replace(".addr", "") in var_def_line_dict:
                            var_def_line_dict[i.text.replace(".addr", "")] = [i.get("defLine")]
                        else:
                            var_def_line_dict[i.text.replace(".addr", "")].append(i.get("defLine"))
                            var_def_line_dict[i.text.replace(".addr", "")] = list(
                                set(var_def_line_dict[i.text.replace(".addr", "")]))
                except AttributeError:
                    pass
    return var_def_line_dict


def __suggest_barriers_for_uncovered_tasks_before_return(pet: PETGraphX, suggestions: [PatternInfo]):
    """enforces taskwait or similar pragmas before return statements to ensure, that no unfinished tasks exist
    when the parent function returns.
    :param pet: PET graph
    :param suggestions; List[PatternInfo]
    :return List[PatternInfo]"""
    # iterate over task suggestions
    for suggestion in suggestions:
        if suggestion.pragma[0] != "task":
            continue
        # if task is covered by a parallel region, ignore it due to the present, implicit barrier
        covered_by_parallel_region = False
        for tmp in suggestions:
            if type(tmp) == ParallelRegionInfo:
                if __line_contained_in_region(suggestion.start_line, tmp.region_start_line, tmp.region_end_line):
                    covered_by_parallel_region = True
                    break
        if covered_by_parallel_region:
            continue
        # check, if barrier in successor - path between task and return (same cu -> no barrier contained)
        queue = [suggestion._node]
        visited = []
        targets = []
        while len(queue) != 0:
            current_node = queue.pop()
            visited.append(current_node)
            if current_node.tp_contains_taskwait:
                # stop search on this path
                continue
            # check if returnInstructionCount > 0
            if current_node.return_instructions_count > 0:
                # taskwait missing -> add current node to targets
                targets.append(current_node)
                continue
            # append direct successors to targets, if not in visited
            successors = pet.direct_successors(current_node)
            successors = [ds for ds in successors if ds not in visited]
            queue = queue + successors
        # suggest taskwait prior to return if needed
        for cu in targets:
            # actual change
            cu.tp_contains_taskwait = True
            pragma_line = cu.end_position()  # since return has to be the last statement in a CU
            pragma_line = pragma_line[pragma_line.index(":") + 1:]
            tmp_suggestion = TaskParallelismInfo(cu,
                                                 ["taskwait"],
                                                 pragma_line,
                                                 [], [], [])
            print("TPDet:suggest_barriers_for_uncovered_tasks_before_return: added taskwait suggestion at line: ",
                  cu.end_position())
            suggestions.append(tmp_suggestion)
    return suggestions


def __filter_data_depend_clauses(pet: PETGraphX, suggestions: [PatternInfo], var_def_line_dict: dict):
    """Removes superfluous variables from the data depend clauses
    of task suggestions.
    :param pet: PET graph
    :param suggestions: List[PatternInfo]
    :return List[PatternInfo]
    """
    for suggestion in suggestions:
        # only consider task suggestions
        if suggestion.pragma[0] != "task" and suggestion.pragma[0] != "taskloop":
            continue
        # get function containing the task cu
        parent_function, last_node = __get_parent_of_type(pet, suggestion._node, NodeType.FUNC, EdgeType.CHILD, True)[0]
        # filter in_dep
        to_be_removed = []
        for var in suggestion.in_dep:
            var = var.replace(".addr", "")
            is_valid = False
            try:
                for defLine in var_def_line_dict[var]:
                    # ensure backwards compatibility (no definition line present in cu_xml
                    if defLine is None:
                        is_valid = True
                    # check if var is defined in parent function
                    if __line_contained_in_region(defLine, parent_function.start_position(),
                                                  parent_function.end_position()):
                        is_valid = True
                    else:
                        pass
            except ValueError:
                pass
            if not is_valid:
                to_be_removed.append(var)
        to_be_removed = list(set(to_be_removed))
        suggestion.in_dep = [v for v in suggestion.in_dep if not v.replace(".addr", "") in to_be_removed]
        # filter out_dep
        to_be_removed = []
        for var in suggestion.out_dep:
            var = var.replace(".addr", "")
            is_valid = False
            try:
                for defLine in var_def_line_dict[var]:
                    # ensure backwards compatibility (no definition line present in cu_xml
                    if defLine is None:
                        is_valid = True
                    # check if var is defined in parent function
                    if __line_contained_in_region(defLine, parent_function.start_position(),
                                                  parent_function.end_position()):
                        is_valid = True
                    else:
                        pass
            except ValueError:
                pass
            if not is_valid:
                to_be_removed.append(var)
        to_be_removed = list(set(to_be_removed))
        suggestion.out_dep = [v for v in suggestion.out_dep if not v.replace(".addr", "") in to_be_removed]
        # filter in_out_dep
        to_be_removed = []
        for var in suggestion.in_out_dep:
            var = var.replace(".addr", "")
            is_valid = False
            try:
                for defLine in var_def_line_dict[var]:
                    # ensure backwards compatibility (no definition line present in cu_xml
                    if defLine is None:
                        is_valid = True
                    # check if var is defined in parent function
                    if __line_contained_in_region(defLine, parent_function.start_position(),
                                                  parent_function.end_position()):
                        is_valid = True
                    else:
                        pass
            except ValueError:
                pass
            if not is_valid:
                to_be_removed.append(var)
        to_be_removed = list(set(to_be_removed))
        suggestion.in_out_dep = [v for v in suggestion.in_out_dep if not v.replace(".addr", "") in to_be_removed]
    return suggestions


def __filter_data_sharing_clauses(pet: PETGraphX, suggestions: [PatternInfo], var_def_line_dict: dict):
    """Removes superfluous variables from the data sharing clauses
    of task suggestions.
    :param pet: PET graph
    :param suggestions: List[PatternInfo]
    :return List[PatternInfo]
    """
    for suggestion in suggestions:
        # only consider task suggestions
        if suggestion.pragma[0] != "task" and suggestion.pragma[0] != "taskloop":
            continue
        # get function containing the task cu
        parent_function, last_node = __get_parent_of_type(pet, suggestion._node, NodeType.FUNC, EdgeType.CHILD, True)[0]
        # filter firstprivate
        to_be_removed = []
        for var in suggestion.first_private:
            var = var.replace(".addr", "")
            is_valid = False
            try:
                for defLine in var_def_line_dict[var]:
                    # ensure backwards compatibility (no definition line present in cu_xml
                    if defLine is None:
                        is_valid = True
                    # check if var is defined in parent function
                    if __line_contained_in_region(defLine, parent_function.start_position(),
                                                  parent_function.end_position()):
                        is_valid = True
                    else:
                        pass
            except ValueError:
                pass
            if not is_valid:
                to_be_removed.append(var)
        to_be_removed = list(set(to_be_removed))
        suggestion.first_private = [v for v in suggestion.first_private if not v.replace(".addr", "") in to_be_removed]
        # filter private
        to_be_removed = []
        for var in suggestion.private:
            var = var.replace(".addr", "")
            is_valid = False
            try:
                for defLine in var_def_line_dict[var]:
                    # catch GlobalVar and LineNotFound
                    if defLine == "GlobalVar":
                        is_valid = True
                        continue
                    if defLine == "LineNotFound":
                        continue
                    # ensure backwards compatibility (no definition line present in cu_xml
                    if defLine is None:
                        is_valid = True
                    # check if var is defined in parent function
                    elif __line_contained_in_region(defLine, parent_function.start_position(),
                                                    parent_function.end_position()):
                        is_valid = True
                    else:
                        pass
            except ValueError:
                pass
            if not is_valid:
                to_be_removed.append(var)
        to_be_removed = list(set(to_be_removed))
        suggestion.private = [v for v in suggestion.private if not v.replace(".addr", "") in to_be_removed]
        # filter shared
        to_be_removed = []
        for var in suggestion.shared:
            var = var.replace(".addr", "")
            is_valid = False
            try:
                for def_line in var_def_line_dict[var]:
                    # ensure backwards compatibility (no definition line present in cu_xml
                    if def_line == "GlobalVar" or def_line == "LineNotFound":  # TODO: not sure if break is suitable
                        is_valid = False
                        break
                    if def_line is None:
                        is_valid = True
                    # check if var is defined in parent function
                    if __line_contained_in_region(def_line, parent_function.start_position(),
                                                  parent_function.end_position()):
                        is_valid = True
                    else:
                        pass
            except ValueError as ve:
                raise ve
                pass
            if not is_valid:
                to_be_removed.append(var)
        to_be_removed = list(set(to_be_removed))
        suggestion.shared = [v for v in suggestion.shared if not v.replace(".addr", "") in to_be_removed]
    return suggestions


def __suggest_missing_barriers_for_global_vars(pet: PETGraphX, suggestions: [PatternInfo]):
    """Suggests a barrier if a node is a successor of a task CU
    which is not covered by an existing barrier and the set of global variables
    of the CU and the task are overlapping
    (i.e. both CUs access common global variables).
    If the cu which would be suggested as a barrier contains a Task suggestion
    already, ignore the barrier suggestion
    (reason: false positives due to copying of global / local variables in preprocessor).
    :param pet: PET graph
    :param suggestions: List[PatternInfo]
    :return List[PatternInfo]
    """
    # split suggestions into task and taskwait suggestions
    taskwait_suggestions = []
    task_suggestions = []
    for single_suggestion in suggestions:
        if type(single_suggestion) == ParallelRegionInfo or \
                type(single_suggestion) == OmittableCuInfo:
            continue
        elif single_suggestion.pragma[0] == "taskwait":
            taskwait_suggestions.append(single_suggestion)
        elif single_suggestion.pragma[0] == "task":
            task_suggestions.append(single_suggestion)

    # iterate over task suggestions
    for task_sug in task_suggestions:
        visited_nodes = [task_sug._node]
        out_succ_edges = [(s, t, e) for s, t, e in pet.out_edges(task_sug._node.id) if
                          e.etype == EdgeType.SUCCESSOR and
                          pet.node_at(t) != task_sug._node]
        queue = out_succ_edges
        # iterate over queued successor-edges
        while len(queue) > 0:
            succ_edge = queue.pop()
            if not pet.node_at(succ_edge[1]) in visited_nodes:
                visited_nodes.append(pet.node_at(succ_edge[1]))
            else:
                continue
            # if barrier is encountered, stop
            if pet.node_at(succ_edge[1]).tp_contains_taskwait is True:
                continue
            # if edge.target has common global variable with task
            common_vars = [var for
                           var in pet.node_at(succ_edge[1]).global_vars
                           if var in task_sug._node.global_vars]
            if len(common_vars) > 0:
                # if cu is a task suggestion, continue
                if pet.node_at(succ_edge[1]).tp_contains_task is True:
                    continue
                # suggest taskwait
                if pet.node_at(succ_edge[1]).tp_contains_taskwait is False:
                    # actual change
                    pet.node_at(succ_edge[1]).tp_contains_taskwait = True
                    first_line = pet.node_at(succ_edge[1]).start_position()
                    first_line = first_line[first_line.index(":") + 1:]
                    tmp_suggestion = TaskParallelismInfo(pet.node_at(succ_edge[1]),
                                                         ["taskwait"],
                                                         first_line,
                                                         [], [], [])
                    suggestions.append(tmp_suggestion)
                continue
            # append current nodes outgoing successor edges to queue
            target_out_succ_edges = [(s, t, e) for s, t, e in pet.out_edges(pet.node_at(succ_edge[1]).id) if
                                     e.etype == EdgeType.SUCCESSOR and
                                     pet.node_at(t) != pet.node_at(succ_edge[1])]
            queue = list(set(queue + target_out_succ_edges))
    return suggestions


def __validate_barriers(pet: PETGraphX, suggestions: [PatternInfo]):
    """Checks if >= 2 dependencies exist from same successor path or
    node that contains the barrier is of type loop.
    Eliminate those barrier suggestions that violate this requirement.
    A successor path is represented by a list of nodes reachable by traversing
    the successor edges inside a single function in reverse direction.
    Note, that nodes with multiple outgoing successor edges
    (multiple control flow options) lead to a separation of the created
    successor paths to support the desired behavior.
    :param pet: PET graph
    :param suggestions: List[PatternInfo]
    :return List[PatternInfo]
    """
    barrier_suggestions = []
    result = []
    for single_suggestion in suggestions:
        try:
            if single_suggestion.pragma[0] == "taskwait":
                barrier_suggestions.append(single_suggestion)
            else:
                result.append(single_suggestion)
        except AttributeError:
            result.append(single_suggestion)

    for bs in barrier_suggestions:
        # check if type of bs node is loop and accept the suggestion if so
        # reason: if task is spawned inside a loop, paths are irrelevant
        if bs._node.type == NodeType.LOOP:
            result.append(bs)
            continue

        # create "path lists" for each incoming successor edge
        in_succ_edges = [(s, t, e) for s, t, e in pet.in_edges(bs._node.id) if
                         e.etype == EdgeType.SUCCESSOR and
                         pet.node_at(s) != bs._node]
        predecessors_dict = dict()
        for e in in_succ_edges:
            visited_nodes = []
            tmp, visited_nodes = __get_predecessor_nodes(pet, pet.node_at(e[0]), visited_nodes)
            predecessors_dict[e] = tmp
        # iterate over outgoing dependence edges and increase dependence counts
        # for those paths that contain the dependence target CU
        out_dep_edges = [(s, t, e) for s, t, e in pet.out_edges(bs._node.id) if
                         e.etype == EdgeType.DATA and
                         pet.node_at(t) != bs._node]
        dependence_count_dict = dict()

        for key in predecessors_dict:
            dependence_count_dict[key] = 0

        for key in predecessors_dict:
            for e in out_dep_edges:
                if pet.node_at(e[1]) in predecessors_dict[key]:
                    dependence_count_dict[key] += 1

        # if validated, append bs to result
        validation_successful = False
        for key in dependence_count_dict:
            if dependence_count_dict[key] > 1:
                result.append(bs)
                validation_successful = True
                break
        # if not validated, unmark node as containing a taskwait in the graph
        if not validation_successful:
            bs._node.tp_contains_taskwait = False

    return result


def __get_predecessor_nodes(pet: PETGraphX, root: CUNode, visited_nodes: [CUNode]):
    """return a list of reachable predecessor nodes.
    generate list recursively.
    stop recursion if a node of type "function" is found or root is a barrier
    (predecessing barrier of the original root node, further predecessors are
    already covered by this barrier and thus can be ignored)."""
    result = [root]
    visited_nodes.append(root)
    if root.type == NodeType.FUNC or root.tp_contains_taskwait is True:
        # root of type "function" or root is a barrier
        return result, visited_nodes
    in_succ_edges = [(s, t, e) for s, t, e in pet.in_edges(root.id) if
                     e.etype == EdgeType.SUCCESSOR and
                     pet.node_at(s) != root and pet.node_at(s) not in visited_nodes]
    for e in in_succ_edges:
        tmp, visited_nodes = __get_predecessor_nodes(pet, pet.node_at(e[0]), visited_nodes)
        result += tmp

    return result, visited_nodes


def __remove_duplicates(suggestions: [PatternInfo]):
    """removes duplicates from the list of suggestions and return the modified
    list.
    CU-ID is not considered.
    Removes a suggestion, if one with identical region_start_line,
    region_end_line and pragma exists.
    :param suggestions: List[PatternInfo]
    :return List[PatternInfo]
    """
    buffer = []  # list of tuples containing region_start_line,
    # region_end_line and pragma, representing suggestions
    result = []
    for sug in suggestions:
        representing_tuple = (sug.region_start_line,
                              sug.region_end_line,
                              sug.pragma)
        if representing_tuple in buffer:
            continue
        else:
            buffer.append(representing_tuple)
            result.append(sug)
    return result


def __sort_output(suggestions: [PatternInfo]):
    """orders the list of suggestions by the respective properties:
    order by: file-id, then line-number (descending).
    Returns the sorted list of suggestions
    :param suggestions: List[PatternInfo]
    :return List[PatternInfo]
    """
    sorted_suggestions = []
    tmp_dict = dict()
    for sug in suggestions:
        # get start_line and file_id for sug
        if ":" not in sug.region_start_line:
            start_line = sug.region_start_line
            file_id = sug.start_line[0:sug.start_line.index(":")]
        else:
            start_line = sug.region_start_line
            file_id = start_line[0:start_line.index(":")]
            start_line = start_line[start_line.index(":") + 1:]
        # split suggestions by file-id
        if file_id not in tmp_dict:
            tmp_dict[file_id] = []
        tmp_dict[file_id].append((start_line, sug))
    # sort suggestions by line-number (descending)
    for key in tmp_dict:
        sorted_list = sorted(tmp_dict[key], key=lambda x: x[0], reverse=True)
        sorted_list = [elem[1] for elem in sorted_list]
        sorted_suggestions += sorted_list
    return sorted_suggestions


def __detect_task_suggestions(pet: PETGraphX):
    """creates task parallelism suggestions and returns them as a list of
    TaskParallelismInfo objects.
    Currently relies on previous processing steps and suggests WORKER CUs
    as Tasks and BARRIER/BARRIER_WORKER as Taskwaits.
    :param pet: PET graph
    :return List[TaskParallelismInfo]
    """
    # suggestions contains a map from LID to a set of suggestions. This is required to
    # detect multiple suggestions for a single line of source code.
    suggestions = dict()  # LID -> List[TaskParallelismInfo]

    # get a list of cus classified as WORKER
    worker_cus = []
    barrier_cus = []
    barrier_worker_cus = []

    func_cus = []

    for v in pet.all_nodes():
        if v.mw_type == MWType.WORKER:
            worker_cus.append(v)
        if v.mw_type == MWType.BARRIER:
            barrier_cus.append(v)
        if v.mw_type == MWType.BARRIER_WORKER:
            barrier_worker_cus.append(v)
        if v.type == NodeType.FUNC:
            func_cus.append(v)

    worker_cus = worker_cus + barrier_worker_cus + func_cus

    # SUGGEST TASKWAIT
    for v in barrier_cus:
        # get line number of first dependency. suggest taskwait prior to that
        first_dependency_line = v.end_position()
        first_dependency_line_number = first_dependency_line[
            first_dependency_line.index(":") + 1:]
        for s, t, e in pet.out_edges(v.id):
            if e.etype == EdgeType.DATA:
                dep_line = e.sink
                dep_line_number = dep_line[dep_line.index(":") + 1:]
                if dep_line_number < first_dependency_line_number:
                    first_dependency_line = dep_line
        tmp_suggestion = TaskParallelismInfo(v, ["taskwait"],
                                             first_dependency_line,
                                             [], [], [])
        if v.start_position() not in suggestions:
            # no entry for source code line contained in suggestions
            tmp_set = []
            suggestions[v.start_position()] = tmp_set
            suggestions[v.start_position()].append(tmp_suggestion)
        else:
            # entry for source code line already contained in suggestions
            suggestions[v.start_position()].append(tmp_suggestion)

    # SUGGEST TASKS
    for vx in pet.all_nodes():
        # iterate over all entries in recursiveFunctionCalls
        # in order to find task suggestions
        for i in range(0, len(vx.recursive_function_calls)):
            function_call_string = vx.recursive_function_calls[i]
            if not type(function_call_string) == str:
                continue
            contained_in = __recursive_function_call_contained_in_worker_cu(function_call_string, worker_cus)
            if contained_in is not None:
                current_suggestions = None
                # recursive Function call contained in worker cu
                # -> issue task suggestion
                pragma_line = function_call_string[
                    function_call_string.index(":") + 1:]
                pragma_line = pragma_line.replace(",", "").replace(" ", "")

                # only include cu and func nodes
                if not (contained_in.type == NodeType.FUNC or
                        contained_in.type == NodeType.CU):
                    print("contained in ", contained_in, "  type: ", contained_in.type)
                    continue
                if contained_in.mw_type == MWType.WORKER or \
                        contained_in.mw_type == MWType.BARRIER_WORKER or \
                        contained_in.type == NodeType.FUNC:
                    # suggest task
                    fpriv, priv, shared, in_dep, out_dep, in_out_dep, red = \
                        classify_task_vars(pet, contained_in, "", [], [])
                    current_suggestions = TaskParallelismInfo(vx, ["task"],
                                                              pragma_line,
                                                              [v.name for v in fpriv],
                                                              [v.name for v in priv],
                                                              [v.name for v in shared])

                # insert current_suggestions into suggestions
                # check, if current_suggestions contains an element
                if current_suggestions is not None:
                    # current_suggestions contains something
                    if pragma_line not in suggestions:
                        # LID not contained in suggestions
                        tmp_set = []
                        suggestions[pragma_line] = tmp_set
                        suggestions[pragma_line].append(current_suggestions)
                    else:
                        # LID already contained in suggestions
                        suggestions[pragma_line].append(current_suggestions)
    # end of for loop

    # construct return value (list of TaskParallelismInfo)
    result = []
    for key in suggestions:
        for single_suggestion in suggestions[key]:
            result.append(single_suggestion)
    return result


def __combine_omittable_cus(pet: PETGraphX,
                            suggestions: [PatternInfo]):
    """execute combination of tasks suggestions with omittable cus.
    Adds modified version of the respective Parent suggestions to the list.
    Returns the modified list of suggestions.
    Omittable CU suggetions are removed from the list.
    Removes duplicates in in/out/in_out dependency lists.
    :param pet: PET graph
    :param suggestions: List [PatternInfo]
    :return List[PatternInfo]
    """
    omittable_suggestions = []
    task_suggestions = []
    result = []
    for single_suggestion in suggestions:
        if type(single_suggestion) == OmittableCuInfo:
            omittable_suggestions.append(single_suggestion)
        else:
            try:
                if single_suggestion.pragma[0] == "task":
                    task_suggestions.append(single_suggestion)
                else:
                    result.append(single_suggestion)
            except AttributeError:
                result.append(single_suggestion)

    # remove omittable suggestion if cu is no direct child in the
    # successor graph of a node containing a task suggestion
    useful_omittable_suggestions = []
    for oms in omittable_suggestions:
        in_succ_edges = [(s, t, e) for s, t, e in pet.in_edges(oms._node.id) if
                         e.etype == EdgeType.SUCCESSOR]
        parent_task_nodes = [pet.node_at(e[0]) for e in in_succ_edges if
                             pet.node_at(e[0]).tp_contains_task is True]
        if len(parent_task_nodes) != 0:
            useful_omittable_suggestions.append(oms)
        else:
            # un-mark node as omittable
            oms._node.tp_omittable = False
    omittable_suggestions = useful_omittable_suggestions

    # create copies of original Task suggestion versions
    for omit_s in omittable_suggestions:
        for ts in task_suggestions:
            if omit_s.combine_with_node == ts._node:
                result.append(copy.copy(ts))

    # prepare dict to find target suggestions for combination
    task_suggestions_dict = dict()
    for ts in task_suggestions:
        if ts._node in task_suggestions_dict:
            task_suggestions_dict[ts._node].append(ts)
        else:
            task_suggestions_dict[ts._node] = [ts]

    for omit_s in omittable_suggestions:
        # process in_out dependencies of omit_s
        # -> lazy, let following statements take care
        for omit_in_out_var in omit_s.in_out_dep:
            omit_s.in_dep.append(omit_in_out_var)
            omit_s.out_dep.append(omit_in_out_var)

        # find target task_suggestion for omit_s, based on in / out dep matches
        omit_target_task_indices = []
        if omit_s.combine_with_node in task_suggestions_dict:
            if len(task_suggestions_dict[omit_s.combine_with_node]) != 1:
                # search for matching in/out dependency pair
                for idx, ts in enumerate(task_suggestions_dict[omit_s.combine_with_node]):
                    intersect = [v for v in omit_s.in_dep if v in ts.out_dep]
                    if len(intersect) == len(omit_s.in_dep):
                        # all in_deps covered
                        omit_target_task_indices.append(idx)
            else:
                omit_target_task_indices = [0]

            for omit_target_task_idx in omit_target_task_indices:
                # note: dependencies of task nodes can contain multiples
                # process out dependencies of omit_s
                for omit_out_var in omit_s.out_dep:
                    task_suggestions_dict[omit_s.combine_with_node][
                        omit_target_task_idx].out_dep.append(omit_out_var)
                    # omit_s.combine_with_node.out_dep.append(omit_out_var)
                # process in dependencies of omit_s
                for omit_in_var in omit_s.in_dep:
                    # note: only dependencies to target node allowed
                    if omit_in_var in task_suggestions_dict[omit_s.combine_with_node][
                            omit_target_task_idx].out_dep:
                        task_suggestions_dict[omit_s.combine_with_node][
                            omit_target_task_idx].out_dep.remove(omit_in_var)
                    # omit_s.combine_with_node.out_dep.remove(omit_in_var)

                # increase size of pragma region if needed
                if int(omit_s.end_line[omit_s.end_line.index(":") + 1:]) > \
                        int(task_suggestions_dict[omit_s.combine_with_node][
                            omit_target_task_idx].region_end_line):
                    task_suggestions_dict[omit_s.combine_with_node][
                        omit_target_task_idx].region_end_line = omit_s.end_line

    # remove duplicates from dependency lists and append to result
    for key in task_suggestions_dict:
        for ts in task_suggestions_dict[key]:
            # remove duplicates
            ts.in_dep = list(set(ts.in_dep))
            ts.out_dep = list(set(ts.out_dep))
            # reset in_out_dep, might have changed due to combination
            if len(ts.in_dep) < len(ts.out_dep):  # just for performance
                ts.in_out_dep = [var for var in ts.in_dep if var in ts.out_dep]
            else:
                ts.in_out_dep = [var for var in ts.out_dep if var in ts.in_dep]
            ts.in_out_dep = list(set(ts.in_out_dep))
            result.append(ts)

    return result


def __detect_dependency_clauses(pet: PETGraphX,
                                suggestions: [PatternInfo]):
    """detect in, out and inout dependencies for tasks and omittable CUs and
    add this information to the respective suggestions.
    dependencies are written into a list, result in multiple entries for a
    value in case of multiple dependencies.
    Return the modified list of suggestions.
    :param pet: PET graph
    :param suggestions: List[PatternInfo]
    :return List[PatternInfo]
    """
    omittable_suggestions = []
    task_suggestions = []
    result = []
    for single_suggestion in suggestions:
        if type(single_suggestion) == OmittableCuInfo:
            omittable_suggestions.append(single_suggestion)
        else:
            try:
                if single_suggestion.pragma[0] == "task":
                    task_suggestions.append(single_suggestion)
                else:
                    result.append(single_suggestion)
            except AttributeError:
                result.append(single_suggestion)

    for s in omittable_suggestions + task_suggestions:
        # out/in_dep_edges are based on the dependency graph and thus inverse
        # to the omp dependency clauses
        # only consider those dependencies to/from Task/Omittable CUs
        out_dep_edges = [(src, t, e) for src, t, e in pet.out_edges(s._node.id) if
                         e.etype == EdgeType.DATA and
                         (pet.node_at(t).tp_contains_task is True or
                          pet.node_at(t).tp_omittable is True) and
                         pet.node_at(t) != s._node]  # exclude self-dependencies
        in_dep_edges = [(src, t, e) for src, t, e in pet.in_edges(s._node.id) if
                        e.etype == "dependence" and
                        (pet.node_at(t).tp_contains_task is True or
                         pet.node_at(t).tp_omittable is True) and
                        pet.node_at(t) != s._node]  # exclude self-dependencies
        # set inverted dependencies
        length_in = 0
        length_out = 0
        for ode in out_dep_edges:
            var = ode[2].var_name
            s.in_dep.append(var)
        for ide in in_dep_edges:
            var = ide[2].var_name
            s.out_dep.append(var)
        # find and set in_out_dependencies
        if length_in < length_out:  # just for performance
            s.in_out_dep = [var for var in s.in_dep if var in s.out_dep]
        else:
            s.in_out_dep = [var for var in s.out_dep if var in s.in_dep]
        # remove in_out_deps from in_dep and out_dep
        for in_out_var in s.in_out_dep:
            s.in_dep = [var for var in s.in_dep if not var == in_out_var]
            s.out_dep = [var for var in s.out_dep if not var == in_out_var]
        result.append(s)
    return result


def __detect_barrier_suggestions(pet: PETGraphX,
                                 suggestions: [TaskParallelismInfo]):
    """detect barriers which have not been detected by __detect_mw_types,
    especially marks WORKER as BARRIER_WORKER if it has depencies to two or
    more CUs which are contained in a path to a CU containing at least one
    suggested Task.
    If omittable CUs are found in the process, they will be marked in the
    pet graph and an intermediate entry in suggestions will be created.
    function executed is repeated until convergence.
    steps:
    1.) mark node as Barrier, if dependences only to task-containing-paths
    :param pet: PET Graph
    :param suggestions: List[TaskParallelismInfo]
    :return List[TaskParallelismInfo]
    """
    # split suggestions into task and taskwait suggestions
    taskwait_suggestions = []
    task_suggestions = []
    omittable_suggestions = []
    for single_suggestion in suggestions:
        if type(single_suggestion) == ParallelRegionInfo:
            continue
        elif type(single_suggestion) == OmittableCuInfo:
            omittable_suggestions.append(single_suggestion)
        elif single_suggestion.pragma[0] == "taskwait":
            taskwait_suggestions.append(single_suggestion)
        elif single_suggestion.pragma[0] == "task":
            task_suggestions.append(single_suggestion)
    for s in task_suggestions:
        s._node.tp_contains_task = True
    for s in taskwait_suggestions:
        s._node.tp_contains_taskwait = True
    task_nodes = [t._node for t in task_suggestions]
    barrier_nodes = [t._node for t in taskwait_suggestions]
    omittable_nodes = []

    transformation_happened = True
    # let run until convergence
    queue = list(pet.all_nodes())
    while transformation_happened or len(queue) > 0:
        transformation_happened = False
        v = queue.pop(0)
        # check step 1
        out_dep_edges = [(s, t, e) for s, t, e in pet.out_edges(v.id) if
                         e.etype == EdgeType.DATA and
                         pet.node_at(t) != v]
        # ignore cyclic dependencies on the same variable
        to_remove = []
        for dep_edge in out_dep_edges:
            targets_cyclic_dep_edges = [(s, t, e) for s, t, e in pet.out_edges(dep_edge[1]) if
                                        e.etype == EdgeType.DATA and
                                        t == dep_edge[0] and
                                        e.var_name == dep_edge[2].var_name]
            if len(targets_cyclic_dep_edges) != 0:
                to_remove.append(dep_edge)
        for e in to_remove:
            out_dep_edges.remove(e)

        v_first_line = v.start_position()
        v_first_line = v_first_line[v_first_line.index(":") + 1:]
        task_count = 0
        barrier_count = 0
        omittable_count = 0
        normal_count = 0
        task_buffer = []
        barrier_buffer = []
        omittable_parent_buffer = []
        for e in out_dep_edges:
            if pet.node_at(e[1]) in task_nodes:
                # only count distinct tasks
                if pet.node_at(e[1]) not in task_buffer:
                    task_buffer.append(pet.node_at(e[1]))
                    task_count += 1
                else:
                    pass
            elif pet.node_at(e[1]) in barrier_nodes:
                # only count distinct barriers
                if pet.node_at(e[1]) not in barrier_buffer:
                    barrier_buffer.append(pet.node_at(e[1]))
                    barrier_count += 1
                else:
                    pass
            elif pet.node_at(e[1]) in [tmp[0] for tmp in omittable_nodes]:
                # treat omittable cus like their parent tasks
                tmp_omit_suggestions = [s for s in suggestions if type(s) == OmittableCuInfo]
                parent_task = [tos for tos in tmp_omit_suggestions if tos._node == pet.node_at(e[1])][
                    0].combine_with_node
                if parent_task.id not in omittable_parent_buffer:
                    omittable_parent_buffer.append(parent_task.id)
                    omittable_count += 1
                else:
                    pass
            else:
                normal_count += 1
        if task_count == 1 and barrier_count == 0:
            if not v.tp_omittable:
                # actual change
                v.tp_omittable = True
                combine_with_node = [pet.node_at(e[1]) for e in out_dep_edges if
                                     pet.node_at(e[1]) in task_nodes]
                if len(combine_with_node) < 1:
                    raise ValueError("length combine_with_node < 1!")
                combine_with_node = combine_with_node[0]
                omittable_nodes.append((v, combine_with_node))
                suggestions.append(OmittableCuInfo(v,
                                                   combine_with_node))
                transformation_happened = True
        elif barrier_count != 0 and task_count != 0:
            # check if child barrier(s) cover each child task
            child_barriers = [e[1] for e in out_dep_edges if
                              pet.node_at(e[1]).tp_contains_taskwait is True]
            child_tasks = [pet.node_at(e[1]) for e in out_dep_edges if
                           pet.node_at(e[1]).tp_contains_task is True]
            uncovered_task_exists = False
            for ct in child_tasks:
                ct_start_line = ct.start_position()
                ct_start_line = ct_start_line[ct_start_line.index(":") + 1:]
                ct_end_line = ct.end_position()
                ct_end_line = ct_end_line[ct_end_line.index(":") + 1:]
                # check if ct covered by a barrier
                for cb in child_barriers:
                    cb = pet.node_at(cb)
                    cb_start_line = cb.start_position()
                    cb_start_line = cb_start_line[cb_start_line.index(":") + 1:]
                    cb_end_line = cb.end_position()
                    cb_end_line = cb_end_line[cb_end_line.index(":") + 1:]
                    if not (cb_start_line > ct_start_line and
                            cb_end_line > ct_end_line):
                        uncovered_task_exists = True
            if uncovered_task_exists:
                # suggest barrier
                if v.tp_contains_taskwait is False:
                    # actual change
                    v.tp_contains_taskwait = True
                    barrier_nodes.append(v)
                    transformation_happened = True
                    tmp_suggestion = TaskParallelismInfo(v, ["taskwait"],
                                                         v_first_line,
                                                         [], [], [])
                    suggestions.append(tmp_suggestion)
            else:
                # no barrier needed
                pass
        elif omittable_count == 0 and task_count > 1:  # connected to at least two distinct task nodes
            if v.tp_contains_taskwait is False:
                # actual change
                v.tp_contains_taskwait = True
                barrier_nodes.append(v)
                transformation_happened = True
                tmp_suggestion = TaskParallelismInfo(v, ["taskwait"],
                                                     v_first_line,
                                                     [], [], [])
                suggestions.append(tmp_suggestion)
        if omittable_count == 1 and \
                v.tp_contains_task is False and \
                v.tp_contains_taskwait is False:
            # omittable node appended to prior omittable node
            # get parent task
            parent_task = None
            for e in out_dep_edges:
                if pet.node_at(e[1]).tp_omittable is True:
                    # if tp_omittable is set, a omittable_suggestion has to exists.
                    # find this suggestion and extract combine_with_node
                    found_cwn = False
                    for (tmp_omit, tmp_cwn) in omittable_nodes:
                        if pet.node_at(e[1]) == tmp_omit:
                            parent_task = tmp_cwn
                            found_cwn = True
                    if not found_cwn:
                        raise Exception("No parent task for omittable node found!")
            violation = False
            # check if only dependences to self, parent omittable node or path to target task exists
            for e in out_dep_edges:
                if pet.node_at(e[1]) == v:
                    continue
                elif pet.node_at(e[1]).tp_omittable is True:
                    continue
                elif __check_reachability(pet, parent_task, v, EdgeType.DATA):
                    continue
                else:
                    violation = True
            # check if node is a direct successor of an omittable node or a task node
            in_succ_edges = [(s, t, e) for (s, t, e) in pet.in_edges(v.id) if
                             e.etype == EdgeType.SUCCESSOR]
            is_successor = False
            for e in in_succ_edges:
                if pet.node_at(e[0]).tp_omittable is True:
                    is_successor = True
                elif pet.node_at(e[0]).tp_contains_task is True:
                    is_successor = True
            if not is_successor:
                violation = True
            # suggest omittable cu if no violation occured
            if not violation:
                if v.tp_omittable is False:
                    # actual change
                    v.tp_omittable = True
                    omittable_nodes.append((v, parent_task))
                    suggestions.append(OmittableCuInfo(v,
                                                       parent_task))
                    transformation_happened = True

        # append neighbors of modified node to queue
        if transformation_happened:
            in_dep_edges = [(s, t, e) for s, t, e in pet.in_edges(v.id) if
                            e.etype == EdgeType.DATA and
                            pet.node_at(s) != v]
            for e in out_dep_edges:
                queue.append(pet.node_at(e[1]))
            for e in in_dep_edges:
                queue.append(pet.node_at(e[0]))
            queue = list(set(queue))

    return suggestions


def __detect_taskloop_reduction(pet: PETGraphX,
                                suggestions: [TaskParallelismInfo]):
    """detect suggested tasks which can and should be replaced by
    taskloop reduction.
    return the modified list of suggestions.
    Idea:   1. check if suggested task inside loop body
            2. check if outer loop is reduction loop
                3. if so, build reduction clause and modify suggested task
    :param pet: PET graph
    :param suggestions: List[TaskParallelismInfo]
    :return List[TaskParallelismInfo]
    """
    output = []
    # iterate over suggestions
    for s in suggestions:
        # ignore others than tasks
        if not (type(s) == Task or type(s) == TaskParallelismInfo):
            output.append(s)
            continue
        if not s.pragma[0] == "task":
            continue
        # check if s contained in reduction loop body
        red_vars_entry, red_loop = __task_contained_in_reduction_loop(pet, s)
        if red_vars_entry is None:
            # s not contained in reduction loop body
            output.append(s)
        else:
            # s contained in reduction loop body
            # modify task s
            reduction_clause = "reduction("
            reduction_clause += red_vars_entry["operation"] + ":"
            reduction_clause += red_vars_entry["name"].replace(".addr", "")
            reduction_clause += ")"
            s.pragma = ["taskloop", reduction_clause]
            # update pragma line to parent reduction loop
            s.pragma_line = red_loop.start_position()
            # update pragma region
            s.region_start_line = red_loop.start_position()
            s.region_end_line = red_loop.end_position()
            # append modified task to output
            output.append(s)
    return output


def __task_contained_in_reduction_loop(pet: PETGraphX,
                                       task: TaskParallelismInfo):
    """detect if task is contained in loop body of a reduction loop.
    return None, if task is not contained in reduction loop.
    else, return reduction_vars entry of parent reduction loop and loop CU Node.
    :param pet: PET graph
    :param task: TaskParallelismInfo
    :return None / ({loop_line, name, reduction_line, operation}, CUNode)
    """
    # check if task contained in loop body
    parents = __get_parent_of_type(pet, task._node, NodeType.LOOP, EdgeType.CHILD, False)
    contained_in = []
    if len(parents) == 0:
        return None, None
    else:
        # check if task is actually contained in one of the parents
        for parent_loop, last_node in parents:
            p_start_line = parent_loop.start_position()
            p_start_line = p_start_line[p_start_line.index(":") + 1:]
            p_end_line = parent_loop.end_position()
            p_end_line = p_end_line[p_end_line.index(":") + 1:]
            t_start_line = task.start_line
            t_start_line = t_start_line[t_start_line.index(":") + 1:]
            t_end_line = task.end_line
            t_end_line = t_end_line[t_end_line.index(":") + 1:]
            if p_start_line <= t_start_line and p_end_line >= t_end_line:
                contained_in.append(parent_loop)
    # check if task is contained in a reduction loop
    for parent in contained_in:
        if parent.reduction:
            # get correct entry for loop from pet.reduction_vars
            for rv in pet.reduction_vars:
                if rv["loop_line"] == parent.start_position():
                    return rv, parent
    return None, None


def __set_task_contained_lines(suggestions: [TaskParallelismInfo]):
    """set region_end_line property of TaskParallelismInfo objects
    in suggestions and return the modified list.
    Regions are determined by checking if a CU contains multiple Tasks or
    Barriers and splitting up the contained source code lines accordingly.
    :param suggestions: List[TaskParallelismInfo]
    :return List[TaskParallelismInfo]"""
    # group suggestions by parent CU
    output = []
    cu_to_suggestions_map = dict()
    for s in suggestions:
        # filter out non task / taskwait suggestions and append to output
        if not (type(s) == Task or type(s) == TaskParallelismInfo):
            output.append(s)
            continue
        # fill cu_to_suggestions_map
        if s.node_id in cu_to_suggestions_map:
            cu_to_suggestions_map[s.node_id].append(s)
        else:
            cu_to_suggestions_map[s.node_id] = [s]
    # order suggestions for each CU by first affected line
    for cu in cu_to_suggestions_map:
        sorted = cu_to_suggestions_map[cu]
        sorted.sort(key=lambda s: s.region_start_line)
        cu_to_suggestions_map[cu] = sorted
    # iterate over suggestions. set region_end_line to end of cu or
    # beginning of next suggestion
    for cu in cu_to_suggestions_map:
        for idx, s in enumerate(cu_to_suggestions_map[cu]):
            # check if next element exists
            if idx + 1 < len(cu_to_suggestions_map[cu]):
                # if so, set end to line prior to start of next suggestion
                end = int(cu_to_suggestions_map[cu][idx + 1].region_start_line)
                end = end - 1
                s.region_end_line = end
            else:
                # if not, set end to end of cu
                s.region_end_line = s.end_line[s.end_line.index(":") + 1:]
            # overwrite entry in cu_to_suggestions_map for s
            cu_to_suggestions_map[cu][idx] = s
    # append suggestions to output
    for cu in cu_to_suggestions_map:
        for s in cu_to_suggestions_map[cu]:
            output.append(s)
    return output


def __remove_useless_barrier_suggestions(pet: PETGraphX,
                                         suggestions: [TaskParallelismInfo]):
    """remove suggested barriers which are not contained in the same
    function body with at least one suggested task.
    Returns the filtered version of the list given as a parameter.
    :param pet: PET graph
    :param suggestions: List[TaskParallelismInfo]
    :return List[TaskParallelismInfo]
    """
    # split suggestions into task and taskwait suggestions
    taskwait_suggestions = []
    task_suggestions = []
    result_suggestions = []
    for single_suggestion in suggestions:
        if single_suggestion.pragma[0] == "taskwait":
            taskwait_suggestions.append(single_suggestion)
        elif single_suggestion.pragma[0] == "task":
            task_suggestions.append(single_suggestion)
        else:
            result_suggestions.append(single_suggestion)
    # get map of function body cus containing task suggestions to line number
    # of task pragmas
    relevant_function_bodies = {}
    for ts in task_suggestions:
        # get first parent cu with type function using bfs
        parent = __get_parent_of_type(pet, ts._node, NodeType.FUNC, EdgeType.CHILD, True)
        parent = parent[0][0]  # parent like [(parent, last_node)]
        if parent not in relevant_function_bodies:
            relevant_function_bodies[parent] = [ts.pragma_line]
        else:
            relevant_function_bodies[parent].append(ts.pragma_line)
    # remove suggested barriers which are no descendants of relevant functions
    result_suggestions += task_suggestions
    for tws in taskwait_suggestions:
        tws_line_number = tws.pragma_line
        tws_line_number = tws_line_number[tws_line_number.index(":") + 1:]
        for rel_func_body in relevant_function_bodies.keys():
            if __check_reachability(pet, tws._node, rel_func_body, EdgeType.CHILD):
                # remove suggested barriers where line number smaller than
                # pragma line number of task
                for line_number in relevant_function_bodies[rel_func_body]:
                    if line_number <= tws_line_number:
                        result_suggestions.append(tws)
                        break
    return result_suggestions


def __suggest_parallel_regions(pet: PETGraphX,
                               suggestions: [TaskParallelismInfo]):
    """create suggestions for parallel regions based on suggested tasks.
    Parallel regions are suggested aroung each outer-most function call
    possibly leading to the creation of tasks.
    To obtain these, the child-graph is traversed in reverse,
    starting from each suggested task.
    :param pet: PET graph
    :param suggestions: List[TaskParallelismInfo]
    :return List[TaskParallelismInfo]"""
    # get task suggestions from suggestions
    task_suggestions = [s for s in suggestions if s.pragma[0] == "task"]
    # start search for each suggested task
    parents = []
    for ts in task_suggestions:
        parents += __get_parent_of_type(pet, ts._node, NodeType.FUNC, EdgeType.CHILD, False)
    # remove duplicates
    parents = list(set(parents))
    # get outer-most parents of suggested tasks
    outer_parents = []
    # iterate over entries in parents.
    while len(parents) > 0:
        (p, last_node) = parents.pop(0)
        p_parents = __get_parent_of_type(pet, p, NodeType.FUNC, EdgeType.CHILD, False)
        if not p_parents:  # p_parents is empty
            # p is outer
            # get last cu before p
            outer_parents.append((p, last_node))
        else:
            # append p´s parents to queue, filter out entries if already
            # present in outer_parents
            first_elements = [x[0] for x in outer_parents]
            parents += [x for x in p_parents if x[0] not in first_elements]

    # create region suggestions based on detected outer parents
    region_suggestions = []
    for parent, last_node in outer_parents:
        region_suggestions.append(ParallelRegionInfo(parent,
                                                     last_node.start_position(),
                                                     last_node.end_position()))
    return region_suggestions


def __check_reachability(pet: PETGraphX, target: CUNode,
                         source: CUNode, edge_type: EdgeType):
    """check if target is reachable from source via edges of type edge_type.
    :param pet: PET graph
    :param source: CUNode
    :param target: CUNode
    :param edge_type: EdgeType
    :return Boolean"""
    visited = []
    queue = [target]
    while len(queue) > 0:
        cur_node = queue.pop(0)
        visited.append(cur_node)
        tmp_list = [(s, t, e) for s, t, e in pet.in_edges(cur_node.id)
                    if s not in visited and
                    e.etype == edge_type]
        for e in tmp_list:
            if pet.node_at(e[0]) == source:
                return True
            else:
                if pet.node_at(e[0]) not in visited:
                    queue.append(pet.node_at(e[0]))
    return False


def __get_parent_of_type(pet: PETGraphX, node: CUNode,
                         parent_type: NodeType, edge_type: EdgeType, only_first: bool):
    """return parent cu nodes and the last node of the path to them as a tuple
    for the given node with type parent_type
    accessible via edges of type edge_type.
    :param pet: PET graph
    :param node: CUNode, root for the search
    :param parent_type: NodeType, type of target node
    :param edge_type: EdgeType, type of usable edges
    :param only_first: Bool, if true, return only first parent.
        Else, return first parent for each incoming edge of node.
    :return [(CUNode, CUNode)]"""
    visited = []
    queue = [(node, None)]
    res = []
    while len(queue) > 0:
        tmp = queue.pop(0)
        (cur_node, last_node) = tmp
        last_node = cur_node
        visited.append(cur_node)
        tmp_list = [(s, t, e) for s, t, e in pet.in_edges(cur_node.id)
                    if pet.node_at(s) not in visited and
                    e.etype == edge_type]
        for e in tmp_list:
            if pet.node_at(e[0]).type == parent_type:
                if only_first is True:
                    return [(pet.node_at(e[0]), last_node)]
                else:
                    res.append((pet.node_at(e[0]), last_node))
                    visited.append(pet.node_at(e[0]))
            else:
                if pet.node_at(e[0]) not in visited:
                    queue.append((pet.node_at(e[0]), last_node))
    return res


def __recursive_function_call_contained_in_worker_cu(function_call_string: str,
                                                     worker_cus: [CUNode]):
    """check if submitted function call is contained in at least one WORKER cu.
    Returns the vertex identifier of the containing cu.
    If no cu contains the function call, None is returned.
    Note: The Strings stored in recursiveFunctionCalls might contain multiple function calls at once.
          in order to apply this function correctly, make sure to split Strings in advance and supply
          one call at a time.
    :param function_call_string: String representation of the recursive function call to be checked
            Ex.: fib 7:35,  (might contain ,)
    :param worker_cus: List of vertices
    """
    # remove , and whitespaces at start / end
    function_call_string = function_call_string.replace(",", "")
    while function_call_string.startswith(" "):
        function_call_string = function_call_string[1:]
    while function_call_string.endswith(" "):
        function_call_string = function_call_string[:-1]
    # function_call_string looks now like like: 'fib 7:52'

    # split String into function_name. file_id and line_number
    file_id = function_call_string[function_call_string.index(" ") + 1:function_call_string.index(":")]
    line_number = function_call_string[function_call_string.index(":") + 1:]

    # get tightest surrounding cu
    tightest_worker_cu = None
    # iterate over worker_cus
    for cur_w in worker_cus:
        cur_w_starts_at_line = cur_w.start_position()
        cur_w_ends_at_line = cur_w.end_position()
        cur_w_file_id = cur_w_starts_at_line[:cur_w_starts_at_line.index(":")]
        # check if file_id is equal
        if file_id == cur_w_file_id:
            # trim to line numbers only
            cur_w_starts_at_line = cur_w_starts_at_line[cur_w_starts_at_line.index(":") + 1:]
            cur_w_ends_at_line = cur_w_ends_at_line[cur_w_ends_at_line.index(":") + 1:]
            # check if line_number is contained
            if int(cur_w_starts_at_line) <= int(line_number) <= int(cur_w_ends_at_line):
                # check if cur_w is tighter than last result
                if tightest_worker_cu is None:
                    tightest_worker_cu = cur_w
                    continue
                if __line_contained_in_region(cur_w.start_position(),
                                              tightest_worker_cu.start_position(),
                                              tightest_worker_cu.end_position()) \
                        and \
                        __line_contained_in_region(cur_w.end_position(),
                                                   tightest_worker_cu.start_position(),
                                                   tightest_worker_cu.end_position()):
                    tightest_worker_cu = cur_w
    return tightest_worker_cu


def __detect_mw_types(pet: PETGraphX, main_node: CUNode):
    """The mainNode we want to compute the Task Parallelism Pattern for it
    use Breadth First Search (BFS) to detect all barriers and workers.
    1.) all child nodes become first worker if they are not marked as worker before
    2.) if a child has dependence to more than one parent node, it will be marked as barrier
    Returns list of BARRIER_WORKER pairs 2
    :param pet: PET graph
    :param main_node: root node
    """

    # first insert all the direct children of main node in a queue to use it for the BFS
    for node in pet.direct_children(main_node):
        # a child node can be set to NONE or ROOT due a former detectMWNode call where it was the mainNode
        if node.mw_type == MWType.NONE or node.mw_type == MWType.ROOT:
            node.mw_type = MWType.FORK

        # while using the node as the base child, we copy all the other children in a copy vector.
        # we do that because it could be possible that two children of the current node (two dependency)
        # point to two different children of another child node which results that the child node becomes BARRIER
        # instead of WORKER
        # so we copy the whole other children in another vector and when one of the children of the current node
        # does point to the other child node, we just adjust mw_type and then we remove the node from the vector
        # Thus we prevent changing to BARRIER due of two dependencies pointing to two different children of
        # the other node

        # create the copy vector so that it only contains the other nodes
        other_nodes = pet.direct_children(main_node)
        other_nodes.remove(node)

        for other_node in other_nodes:
            if depends(pet, other_node, node):
                if other_node.mw_type == MWType.WORKER:
                    other_node.mw_type = MWType.BARRIER
                else:
                    other_node.mw_type = MWType.WORKER

                    # check if other_node has > 1 RAW dependencies to node
                    # -> not detected in previous step, since other_node is only
                    #    dependent of a single CU
                    raw_targets = []
                    for s, t, d in pet.out_edges(other_node.id):
                        if pet.node_at(t) == node:
                            if d.dtype == DepType.RAW:
                                raw_targets.append(t)
                    # remove entries which occur less than two times
                    raw_targets = [t for t in raw_targets if raw_targets.count(t) > 1]
                    # remove duplicates from list
                    raw_targets = list(set(raw_targets))
                    # if elements remaining, mark other_node as BARRIER
                    if len(raw_targets) > 0:
                        other_node.mw_type = MWType.BARRIER

    pairs = []
    # check for Barrier Worker pairs
    # if two barriers don't have any dependency to each other then they create a barrierWorker pair
    # so check every barrier pair that they don't have a dependency to each other -> barrierWorker
    direct_subnodes = pet.direct_children(main_node)
    for n1 in direct_subnodes:
        if n1.mw_type == MWType.BARRIER:
            for n2 in direct_subnodes:
                if n2.mw_type == MWType.BARRIER and n1 != n2:
                    if n2 in [pet.node_at(t) for s, t, d in pet.out_edges(n1.id)] or n2 in [pet.node_at(s) for s, t, d
                                                                                            in pet.in_edges(n1.id)]:
                        break
                    # so these two nodes are BarrierWorker, because there is no dependency between them
                    pairs.append((n1, n2))
                    n1.mw_type = MWType.BARRIER_WORKER
                    n2.mw_type = MWType.BARRIER_WORKER
    # return pairs


def __create_task_tree(pet: PETGraphX, root: CUNode):
    """generates task tree data from root node

    :param pet: PET graph
    :param root: root node
    """
    root_task = Task(pet, root)
    __forks.add(root_task)
    __create_task_tree_helper(pet, root, root_task, [])


def __create_task_tree_helper(pet: PETGraphX, current: CUNode, root: Task, visited_func: List[CUNode]):
    """generates task tree data recursively

    :param pet: PET graph
    :param current: current vertex to process
    :param root: root task for subtree
    :param visited_func: visited function nodes
    """
    if current.type == NodeType.FUNC:
        if current in visited_func:
            return
        else:
            visited_func.append(current)

    for child in pet.direct_children(current):
        mw_type = child.mw_type

        if mw_type in [MWType.BARRIER, MWType.BARRIER_WORKER, MWType.WORKER]:
            task = Task(pet, child)
            root.child_tasks.append(task)
            __create_task_tree_helper(pet, child, task, visited_func)
        elif mw_type == MWType.FORK and not child.start_position().endswith('16383'):
            task = Task(pet, child)
            __forks.add(task)
            __create_task_tree_helper(pet, child, task, visited_func)
        else:
            __create_task_tree_helper(pet, child, root, visited_func)


def cu_xml_preprocessing(cu_xml):
    """Execute CU XML Preprocessing.
    Returns file name of modified cu xml file.
    :param cu_xml: path to the xml file
    :return file name of modified cu xml file.
    """
    xml_fd = open(cu_xml)
    xml_content = ""
    for line in xml_fd.readlines():
        if not (line.rstrip().endswith('</Nodes>') or line.rstrip().endswith('<Nodes>')):
            xml_content = xml_content + line

    xml_content = "<Nodes>{0}</Nodes>".format(xml_content)

    parsed_cu = objectify.fromstring(xml_content)

    iterate_over_cus = True  # used to enable re-starting
    self_added_node_ids = []
    while iterate_over_cus:
        used_node_ids = []
        for node in parsed_cu.Node:
            used_node_ids.append(node.get("id"))

        for node in parsed_cu.Node:
            inner_iteration = True
            remaining_recursive_call_in_parent = False
            while inner_iteration:
                used_node_ids = list(set(used_node_ids + self_added_node_ids))

                if node.get('type') == '0':  # iterate over CU nodes
                    # find CU nodes with > 1 recursiveFunctionCalls in own code region
                    if __preprocessor_cu_contains_at_least_two_recursive_calls(
                            node) or remaining_recursive_call_in_parent:
                        remaining_recursive_call_in_parent = False
                        # Preprocessor Step 1
                        tmp_cn_entry = None  # (recursiveFunctionCall, nodeCalled)
                        for cne_idx, calls_node_entry in enumerate(node.callsNode):
                            # get first matching entry of node.callsNode
                            try:
                                for rc_idx, rec_call in enumerate(calls_node_entry.recursiveFunctionCall):
                                    rec_call_line = calls_node_entry.nodeCalled[rc_idx].get("atLine")
                                    if str(rec_call_line) in str(rec_call):
                                        tmp_cn_entry = (rec_call, calls_node_entry.nodeCalled[rc_idx])
                                        break
                            except AttributeError:
                                continue
                        if tmp_cn_entry is None:
                            raise Exception("no matching entries for callsNode found!")

                        parent = node
                        tmp_cn_entry[0].getparent().remove(tmp_cn_entry[0])
                        tmp_cn_entry[1].getparent().remove(tmp_cn_entry[1])
                        parent_copy = copy.copy(parent)
                        parsed_cu.insert(parsed_cu.index(parent), parent_copy)

                        # Preprocessor Step 2 - generate cu id for new element
                        # get next free id for specific tmp_file_id
                        parent_copy_id = parent_copy.get("id")
                        tmp_file_id = parent_copy_id[:parent_copy_id.index(":")]
                        tmp_used_ids = [int(s[s.index(":") + 1:]) for s in
                                        used_node_ids if
                                        s.startswith(tmp_file_id + ":")]
                        next_free_id = max(tmp_used_ids) + 1
                        incremented_id = tmp_file_id + ":" + str(next_free_id)
                        parent.set("id", incremented_id)
                        self_added_node_ids.append(incremented_id)

                        # Preprocessor Step 3
                        parent_copy.callsNode.clear()
                        parent_copy.callsNode.append(tmp_cn_entry[1])
                        parent_copy.callsNode.append(tmp_cn_entry[0])

                        parent_copy.successors.clear()
                        etree.SubElement(parent_copy.successors, "CU")
                        parent_copy.successors.CU._setText(parent.get("id"))

                        # delete childrenNodes-entry from parent
                        tmp_cu_id = tmp_cn_entry[1].text
                        parent.childrenNodes._setText(parent.childrenNodes.text.replace(tmp_cu_id + ",", ""))
                        parent.childrenNodes._setText(parent.childrenNodes.text.replace(tmp_cu_id, ""))

                        # set parent_copy.childrenNodes
                        parent_copy.childrenNodes._setText("")
                        for cne_idx, calls_node_entry in enumerate(parent_copy.callsNode):
                            try:
                                for node_call in calls_node_entry.nodeCalled:
                                    try:
                                        if node_call.text not in parent_copy.childrenNodes.text:
                                            parent_copy.childrenNodes._setText(
                                                parent_copy.childrenNodes.text + "," + node_call.text)
                                            if parent_copy.childrenNodes.text.startswith(","):
                                                parent_copy.childrenNodes._setText(parent_copy.childrenNodes.text[1:])
                                            if parent_copy.childrenNodes.text.endswith(","):
                                                parent_copy.childrenNodes._setText(parent_copy.childrenNodes.text[:-1])
                                            continue
                                    except AttributeError as e1:
                                        print(e1)
                                        continue
                            except AttributeError as e2:
                                print(e2)
                                continue

                        # Preprocessor Step 4
                        # update startsAtLine and endsAtLine
                        try:
                            if parent_copy.callsNode.nodeCalled.get("atLine") in \
                                    parent.instructionLines.text:
                                parent.instructionLines._setText(parent.instructionLines.text.replace(
                                    parent_copy.callsNode.nodeCalled.get("atLine") + ",", ""))
                                parent.instructionLines._setText(
                                    parent.instructionLines.text.replace(parent_copy.callsNode.nodeCalled.get("atLine"),
                                                                         ""))
                                parent.instructionLines.set("count", str(int(parent.instructionLines.get("count")) - 1))
                        except TypeError:
                            parent.instructionLines._setText(parent_copy.callsNode.nodeCalled.get("atLine"))
                            parent.instructionLines.set("count", "1")

                        try:
                            if parent_copy.callsNode.nodeCalled.get("atLine") in \
                                    parent.readPhaseLines.text:
                                parent.readPhaseLines._setText(parent.readPhaseLines.text.replace(
                                    parent_copy.callsNode.nodeCalled.get("atLine") + ",", ""))
                                parent.readPhaseLines._setText(
                                    parent.readPhaseLines.text.replace(parent_copy.callsNode.nodeCalled.get("atLine"),
                                                                       ""))
                                parent.readPhaseLines.set("count", str(int(parent.readPhaseLines.get("count")) - 1))
                        except TypeError:
                            parent.readPhaseLines._setText(parent_copy.callsNode.nodeCalled.get("atLine"))
                            parent.readPhaseLines.set("count", "1")

                        try:
                            if parent_copy.callsNode.nodeCalled.get("atLine") in \
                                    parent.writePhaseLines.text:
                                parent.writePhaseLines._setText(parent.writePhaseLines.text.replace(
                                    parent_copy.callsNode.nodeCalled.get("atLine") + ",", ""))
                                parent.writePhaseLines._setText(
                                    parent.writePhaseLines.text.replace(parent_copy.callsNode.nodeCalled.get("atLine"),
                                                                        ""))
                                parent.writePhaseLines.set("count", str(int(parent.writePhaseLines.get("count")) - 1))
                        except TypeError:
                            parent.writePhaseLines._setText(parent_copy.callsNode.nodeCalled.get("atLine"))
                            parent.writePhaseLines.set("count", "1")

                        separator_line = parent.get("startsAtLine")
                        # select smallest recursive function call line >= separator_line + 1
                        parent_new_start_line = None
                        potential_lines = []
                        for tmp1 in parent.callsNode:
                            try:
                                for tmp2 in tmp1.nodeCalled:
                                    try:
                                        potential_lines.append(tmp2.get("atLine"))
                                        pass
                                    except AttributeError:
                                        pass
                            except AttributeError:
                                pass
                        for tmp in potential_lines:
                            if tmp == "":
                                continue
                            if int(tmp[tmp.find(":") + 1:]) >= int(separator_line[separator_line.find(":") + 1:]) + 1:
                                if parent_new_start_line is None:
                                    parent_new_start_line = tmp
                                    continue
                                # select smallest instruction line
                                if int(tmp[tmp.find(":") + 1:]) < int(
                                        parent_new_start_line[parent_new_start_line.find(":") + 1:]):
                                    parent_new_start_line = tmp
                        if not potential_lines or (potential_lines and not parent_new_start_line):
                            parent_new_start_line = str(separator_line[:separator_line.index(":")])
                            parent_new_start_line += ":"
                            parent_new_start_line += str(int(separator_line[separator_line.index(":") + 1:]) + 1)

                        parent.set("startsAtLine", parent_new_start_line)
                        parent_copy.set("endsAtLine", separator_line)

                        # update instruction/readPhase/writePhase lines
                        try:
                            for tmp_line in parent_copy.instructionLines.text.split(","):
                                if not __line_contained_in_region(
                                        tmp_line,
                                        parent_copy.get("startsAtLine"),
                                        parent_copy.get("endsAtLine")):
                                    parent_copy.instructionLines._setText(
                                        parent_copy.instructionLines.text.replace(tmp_line + ",", ""))
                                    parent_copy.instructionLines._setText(
                                        parent_copy.instructionLines.text.replace(tmp_line, ""))
                                    if parent_copy.instructionLines.text.endswith(","):
                                        parent_copy.instructionLines._setText(parent_copy.instructionLines.text[:-1])
                                    parent_copy.instructionLines.set("count", str(
                                        int(parent_copy.instructionLines.get("count")) - 1))
                        except AttributeError:
                            pass
                        try:
                            for tmp_line in parent_copy.readPhaseLines.text.split(","):
                                if not __line_contained_in_region(
                                        tmp_line,
                                        parent_copy.get("startsAtLine"),
                                        parent_copy.get("endsAtLine")):
                                    parent_copy.readPhaseLines._setText(
                                        parent_copy.readPhaseLines.text.replace(tmp_line + ",", ""))
                                    parent_copy.readPhaseLines._setText(
                                        parent_copy.readPhaseLines.text.replace(tmp_line, ""))
                                    if parent_copy.readPhaseLines.text.endswith(","):
                                        parent_copy.readPhaseLines._setText(parent_copy.readPhaseLines.text[:-1])
                                    parent_copy.readPhaseLines.set("count", str(
                                        int(parent_copy.readPhaseLines.get("count")) - 1))
                        except AttributeError:
                            pass
                        try:
                            for tmp_line in parent_copy.writePhaseLines.text.split(","):
                                if not __line_contained_in_region(
                                        tmp_line,
                                        parent_copy.get("startsAtLine"),
                                        parent_copy.get("endsAtLine")):
                                    parent_copy.writePhaseLines._setText(
                                        parent_copy.writePhaseLines.text.replace(tmp_line + ",", ""))
                                    parent_copy.writePhaseLines._setText(
                                        parent_copy.writePhaseLines.text.replace(tmp_line, ""))
                                    if parent_copy.writePhaseLines.text.endswith(","):
                                        parent_copy.writePhaseLines._setText(parent_copy.writePhaseLines.text[:-1])
                                    parent_copy.writePhaseLines.set("count", str(
                                        int(parent_copy.writePhaseLines.get("count")) - 1))
                        except AttributeError:
                            pass

                        # insert separator line to parent_copys instruction,
                        # read and writePhaseLines if not already present
                        try:
                            if not parent_copy.get("endsAtLine") in parent_copy.instructionLines.text:
                                parent_copy.instructionLines._setText(
                                    parent_copy.instructionLines.text + "," + parent_copy.get("endsAtLine"))
                                if parent_copy.instructionLines.text.startswith(","):
                                    parent_copy.instructionLines._setText(parent_copy.instructionLines.text[1:])
                                parent_copy.instructionLines.set("count", str(
                                    int(parent_copy.instructionLines.get("count")) + 1))
                        except TypeError:
                            parent_copy.instructionLines._setText(parent_copy.get("endsAtLine"))
                            parent_copy.instructionLines.set("count", "1")
                        try:
                            if not parent_copy.get("endsAtLine") in parent_copy.readPhaseLines.text:
                                parent_copy.readPhaseLines._setText(
                                    parent_copy.readPhaseLines.text + "," + parent_copy.get("endsAtLine"))
                                if parent_copy.readPhaseLines.text.startswith(","):
                                    parent_copy.readPhaseLines._setText(parent_copy.readPhaseLines.text[1:])
                                parent_copy.readPhaseLines.set("count",
                                                               str(int(parent_copy.readPhaseLines.get("count")) + 1))
                        except TypeError:
                            parent_copy.readPhaseLines._setText(parent_copy.get("endsAtLine"))
                            parent_copy.readPhaseLines.set("count", "1")
                        try:
                            if not parent_copy.get("endsAtLine") in parent_copy.writePhaseLines.text:
                                parent_copy.writePhaseLines._setText(
                                    parent_copy.writePhaseLines.text + "," + parent_copy.get("endsAtLine"))
                                if parent_copy.writePhaseLines.text.startswith(","):
                                    parent_copy.writePhaseLines._setText(parent_copy.writePhaseLines.text[1:])
                                parent_copy.writePhaseLines.set("count",
                                                                str(int(parent_copy.writePhaseLines.get("count")) + 1))
                        except TypeError:
                            parent_copy.writePhaseLines._setText(parent_copy.get("endsAtLine"))
                            parent_copy.writePhaseLines.set("count", "1")
                        parent_copy.instructionLines._setText(parent_copy.instructionLines.text.replace(",,", ","))
                        parent_copy.readPhaseLines._setText(parent_copy.readPhaseLines.text.replace(",,", ","))
                        parent_copy.writePhaseLines._setText(parent_copy.writePhaseLines.text.replace(",,", ","))

                        # insert all lines contained in parent to instruction, read and writePhaseLines
                        cur_line = parent.get("startsAtLine")
                        while __line_contained_in_region(cur_line, parent.get("startsAtLine"),
                                                         parent.get("endsAtLine")):
                            if cur_line not in parent.instructionLines.text:
                                parent.instructionLines._setText(cur_line + "," + parent.instructionLines.text)
                                if parent.instructionLines.text.endswith(","):
                                    parent.instructionLines._setText(parent.instructionLines.text[:-1])
                                parent.instructionLines.set("count", str(int(parent.instructionLines.get("count")) + 1))
                            if cur_line not in parent.readPhaseLines.text:
                                parent.readPhaseLines._setText(cur_line + "," + parent.readPhaseLines.text)
                                if parent.readPhaseLines.text.endswith(","):
                                    parent.readPhaseLines._setText(parent.readPhaseLines.text[:-1])
                                parent.readPhaseLines.set("count", str(int(parent.readPhaseLines.get("count")) + 1))
                            if cur_line not in parent.writePhaseLines.text:
                                parent.writePhaseLines._setText(cur_line + "," + parent.writePhaseLines.text)
                                if parent.writePhaseLines.text.endswith(","):
                                    parent.writePhaseLines._setText(parent.writePhaseLines.text[:-1])
                                parent.writePhaseLines.set("count", str(int(parent.writePhaseLines.get("count")) + 1))
                            # increment cur_line by one
                            cur_line = cur_line[0:cur_line.rfind(":") + 1] + str(
                                int(cur_line[cur_line.rfind(":") + 1:]) + 1)
                            continue

                        parent.instructionLines._setText(parent.instructionLines.text.replace(",,", ","))
                        parent.readPhaseLines._setText(parent.readPhaseLines.text.replace(",,", ","))
                        parent.writePhaseLines._setText(parent.writePhaseLines.text.replace(",,", ","))

                        # remove returnInstructions if they are not part of the cus anymore
                        if int(parent_copy.returnInstructions.get("count")) != 0:
                            entries = parent_copy.returnInstructions.text.split(",")
                            new_entries = []
                            for entry in entries:
                                if __line_contained_in_region(entry, parent_copy.get("startsAtLine"),
                                                              parent_copy.get("endsAtLine")):
                                    new_entries.append(entry)
                            parent_copy.returnInstructions._setText(",".join(new_entries))
                            parent_copy.returnInstructions.set("count", str(len(new_entries)))
                        if int(parent.returnInstructions.get("count")) != 0:
                            entries = parent.returnInstructions.text.split(",")
                            new_entries = []
                            for entry in entries:
                                if __line_contained_in_region(entry, parent.get("startsAtLine"),
                                                              parent.get("endsAtLine")):
                                    new_entries.append(entry)
                            parent.returnInstructions._setText(",".join(new_entries))
                            parent.returnInstructions.set("count", str(len(new_entries)))

                        # add parent.id to parent_function.childrenNodes
                        parent_function = None
                        for tmp_node in parsed_cu.Node:
                            if tmp_node.get('type') == '1':
                                if __line_contained_in_region(parent.get("startsAtLine"), tmp_node.get("startsAtLine"),
                                                              tmp_node.get("endsAtLine")):
                                    if __line_contained_in_region(parent.get("endsAtLine"),
                                                                  tmp_node.get("startsAtLine"),
                                                                  tmp_node.get("endsAtLine")):
                                        parent_function = tmp_node
                                        break
                        if parent_function is None:
                            print("No parent function found for cu node: ", parent.get("id"), ". Ignoring.")
                        else:
                            parent_function.childrenNodes._setText(
                                parent_function.childrenNodes.text + "," + parent.get("id"))
                            if parent_function.childrenNodes.text.startswith(","):
                                parent_function.childrenNodes._setText(parent_function.childrenNodes.text[1:])

                        # Preprocessor Step 5 (looping)
                        parent_further_cn_entry = None
                        for cne_idx, calls_node_entry in enumerate(parent.callsNode):
                            # get first matching entry of node.callsNode
                            try:
                                for rc_idx, rec_call in enumerate(calls_node_entry.recursiveFunctionCall):
                                    rec_call_line = calls_node_entry.nodeCalled[rc_idx].get("atLine")
                                    if str(rec_call_line) in str(rec_call):
                                        parent_further_cn_entry = (rec_call, calls_node_entry.nodeCalled[rc_idx])
                                        break
                            except AttributeError:
                                continue
                        if parent_further_cn_entry is None:
                            # parent has no further recursive call, restart outer loop
                            inner_iteration = False
                            continue
                        else:
                            # parent still has recursive calls
                            inner_iteration = True
                            node = parent
                            remaining_recursive_call_in_parent = True
                            continue
                    else:
                        inner_iteration = False
                        continue
                else:
                    # node not of type CU, go to next node
                    inner_iteration = False
                    continue

        iterate_over_cus = False  # disable restarting, preprocessing finished

    # print modified Data.xml to file
    modified_cu_xml = cu_xml.replace(".xml", "-preprocessed.xml")
    if os.path.exists(modified_cu_xml):
        os.remove(modified_cu_xml)
    f = open(modified_cu_xml, "w+")
    f.write(etree.tostring(parsed_cu, pretty_print=True).decode("utf-8"))
    f.close()
    return modified_cu_xml


def __line_contained_in_region(test_line, start_line, end_line):
    """check if test_line is contained in [startLine, endLine].
    Return True if so. False else.
    :param test_line: <fileID>:<line>
    :param start_line: <fileID>:<line>
    :param end_line: <fileID>:<line>
    :return True/False
    """
    test_line_file_id = int(test_line.split(":")[0])
    test_line_line = int(test_line.split(":")[1])
    start_line_file_id = int(start_line.split(":")[0])
    start_line_line = int(start_line.split(":")[1])
    end_line_file_id = int(end_line.split(":")[0])
    end_line_line = int(end_line.split(":")[1])
    if test_line_file_id == start_line_file_id == end_line_file_id and \
            start_line_line <= test_line_line <= end_line_line:
        return True
    return False


def __preprocessor_cu_contains_at_least_two_recursive_calls(node):
    """Check if >= 2 recursive function calls are contained in a cu's code region.
    Returns True, if so.
    Returns False, else.
    :param node: CUNode
    :return True/False
    """
    starts_at_line = node.get("startsAtLine").split(":")
    ends_at_line = node.get("endsAtLine").split(":")
    file_id = starts_at_line[0]
    if file_id != ends_at_line[0]:
        raise Exception("error in Data.xml: FileIds of startsAtLine and endsAtLine not matching!")
    starts_at_line = starts_at_line[1]
    ends_at_line = ends_at_line[1]

    # count contained recursive Function calls
    contained_recursive_calls = 0
    for calls_node_entry in node.callsNode:
        try:
            for i in calls_node_entry.recursiveFunctionCall:
                rec_func_calls = [s for s in str(i).split(",") if len(s) > 0]
                if len(rec_func_calls) != 0:
                    for rec_func_call in rec_func_calls:
                        rec_func_call = rec_func_call.split(" ")[1]
                        rfc_file_id = rec_func_call.split(":")[0]
                        rfc_line = rec_func_call.split(":")[1]
                        # test if recursiveFunctionCall is inside CU region
                        if rfc_file_id == file_id and \
                                starts_at_line <= rfc_line <= ends_at_line:
                            contained_recursive_calls += 1
        except AttributeError:
            pass
    if contained_recursive_calls >= 2:
        return True
    return False
