"""
Workflow Factory - Create LangGraph StateGraph workflows from YAML configuration.

This module provides a factory to build LangGraph workflows from declarative
YAML definitions, making workflow configuration more maintainable.

Note: WorkflowRegistry has been moved to sdk.workflow_registry for better modularity.
      Both import patterns work:
      - from declarative_agent_sdk import WorkflowFactory, WorkflowRegistry  (recommended)
      - from declarative_agent_sdk.workflow_factory import WorkflowFactory
      - from declarative_agent_sdk.workflow_registry import WorkflowRegistry
"""

import yaml
from typing import Any, Dict, Callable, Optional
from pathlib import Path
from langgraph.graph import StateGraph, START, END
from declarative_agent_sdk.workflow_registry import WorkflowRegistry
from declarative_agent_sdk.agent_logging import get_logger
from declarative_agent_sdk import AIWorkflow
from declarative_agent_sdk import AgentState

logger = get_logger(__name__)


class WorkflowFactory:
    """Factory class to create LangGraph StateGraph workflows from YAML configuration."""
    
    @staticmethod
    def from_yaml_file(yaml_file_path: str, state_class: type) -> AIWorkflow:
        """
        Create a StateGraph workflow from a YAML configuration file.
        
        Args:
            yaml_file_path: Path to the YAML configuration file
            state_class: The state class type (e.g., AgentState)
            
        Returns:
            StateGraph instance configured according to the YAML file
            
        Example YAML format:
            name: book_generation_workflow
            description: Multi-agent workflow for book generation
            nodes:
              - name: toc_agent
                function: toc_agent
            edges:
              - from: START
                to: toc_agent
            conditional_edges:
              - from: toc_agent
                router_function: route_after_toc
        """
        yaml_path = Path(yaml_file_path)
        if not yaml_path.exists():
            raise FileNotFoundError(f"YAML file not found: {yaml_file_path}")
        
        with open(yaml_path, 'r') as f:
            config = yaml.safe_load(f)
        
        return WorkflowFactory.from_dict(config, state_class)
    
    @staticmethod
    def from_dict(config: Dict[str, Any], state_class: type) -> AIWorkflow:
        """
        Create a StateGraph workflow from a configuration dictionary.
        
        Args:
            config: Dictionary containing workflow configuration
            state_class: The state class type (e.g., AgentState)
            
        Returns:
            AIWorkflow instance configured according to the dictionary
        """
        name = config.get('name', 'unnamed_workflow')
        logger.info(f"Creating workflow '{name}' from configuration")
        description = config.get('description', 'no description provided')
        
        # Create StateGraph
        workflow = StateGraph(state_class)
        
        # Add nodes
        nodes = config.get('nodes', [])
        for node in nodes:
            node_name = node.get('name')
            function_name = node.get('function')
            
            if not node_name or not function_name:
                raise ValueError(f"Node must have 'name' and 'function': {node}")
            
            # Get function from registry
            try:
                function = WorkflowRegistry.get(function_name)
                workflow.add_node(node_name, function)
                logger.info(f"  Added node: {node_name} -> {function_name}")
            except ValueError as e:
                logger.error(f"Failed to add node '{node_name}': {e}")
                raise
        
        # Add simple edges
        edges = config.get('edges', [])
        for edge in edges:
            from_node = edge.get('from')
            to_node = edge.get('to')
            
            if not from_node or not to_node:
                raise ValueError(f"Edge must have 'from' and 'to': {edge}")
            
            # Handle START and END special nodes
            from_node = START if from_node == 'START' else from_node
            to_node = END if to_node == 'END' else to_node
            
            workflow.add_edge(from_node, to_node)
            logger.info(f"  Added edge: {edge.get('from')} -> {edge.get('to')}")
        
        # Add conditional edges
        conditional_edges = config.get('conditional_edges', [])
        for cond_edge in conditional_edges:
            from_node = cond_edge.get('from')
            router_name = cond_edge.get('router_function')
            
            if not from_node or not router_name:
                raise ValueError(f"Conditional edge must have 'from' and 'router_function': {cond_edge}")
            
            # Get router function from registry
            try:
                router_function = WorkflowRegistry.get(router_name)
                workflow.add_conditional_edges(from_node, router_function)
                logger.info(f"  Added conditional edge: {from_node} -> ({router_name})")
            except ValueError as e:
                logger.error(f"Failed to add conditional edge from '{from_node}': {e}")
                raise
        
        logger.info(f"Workflow '{name}' created successfully")
        return AIWorkflow(name, description, workflow, AgentState)
    
    @staticmethod
    def compile_from_yaml(yaml_file_path: str, state_class: type):
        """
        Create and compile a StateGraph workflow from YAML.
        
        Args:
            yaml_file_path: Path to the YAML configuration file
            state_class: The state class type (e.g., AgentState)
            
        Returns:
            Compiled workflow ready for execution
        """
        workflow = WorkflowFactory.from_yaml_file(yaml_file_path, state_class)
        return workflow._graph.compile()


def register_workflow_functions(functions: Dict[str, Callable]) -> None:
    """
    Register multiple workflow functions at once.
    
    This is a convenience wrapper around WorkflowRegistry.register_multiple().
    
    Args:
        functions: Dictionary mapping function names to callable functions
        
    Example:
        register_workflow_functions({
            'toc_agent': toc_agent,
            'chapter_agent_parallel': chapter_agent_parallel,
            'collation_agent': collation_agent,
            'route_after_toc': route_after_toc
        })
    """
    WorkflowRegistry.register_multiple(functions)
