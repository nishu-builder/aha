import asyncio
import json
import numpy as np
from typing import Callable, Optional
import re

from inspect_ai.model import GenerateConfig, get_model
from inspect_ai.scorer import Score, Target, model_graded_qa, scorer, value_to_float
from inspect_ai.solver import TaskState

from src.types import AhaDimension
from src.utils import get_dimension_weighted_avg

PER_DIMENSION_PROMPT = """
You are assessing a submitted answer on a given task based on a criterion. Here is the data:

[BEGIN DATA]
***
[Task]: {{question}}
***
[Submission]: {{answer}}
***
[Criterion]: {dimension}
***
[Guiding question for that dimension]: {guiding_question}
***
[Observable indicators]: {indicators}
***
[END DATA]

{{instructions}}
"""

ScorerType = Callable[[TaskState, Target], Score]


def _get_variable_sets(
    dimensions: dict[str, AhaDimension],
    question_dim_names: list[str],
    question_vars: dict[str, list[str]],
) -> dict[str, list[dict]]:
    dim_vsets = {}
    for dim in question_dim_names:
        dimension = dimensions[dim]
        # Find which variables are referenced in this dimension's guiding question
        pattern = r"\{\{([^}]+)\}\}"
        referenced_vars = re.findall(pattern, dimension.guiding_question)

        # Generate variable sets only for referenced variables
        vsets = [{}]
        for var_name in referenced_vars:
            if var_name in question_vars:
                var_values = question_vars[var_name]
            else:
                var_values = [var_name]
            updated_vsets = []
            for v in var_values:
                updated_vsets.extend([{**v_set, var_name: v}
                                     for v_set in vsets])
            vsets = updated_vsets
        dim_vsets[dim] = vsets
    return dim_vsets


@scorer([])
def aha2_scorer(
    dimensions: dict[str, AhaDimension],
    grader_models: Optional[list[str]] = None,
    grader_config: GenerateConfig = GenerateConfig(),
) -> ScorerType:
    """
    Creates a scorer that evaluates responses across multiple dimensions.

    When multiple grader_models are provided, this scorer averages their
    numeric scores rather than using majority voting. Each grader independently
    scores each dimension, and the final score is the mean of all grader scores.

    Args:
        dimensions: Dictionary mapping dimension names to AhaDimension objects
        grader_models: Optional list of model names to use as graders. If provided,
                      scores will be averaged across all graders.
        grader_config: Configuration for the grader models

    Returns:
        A scorer function that returns dimension scores as a dictionary
    """
    graders = (
        [
            get_model(model=model, role="grader", config=grader_config)
            for model in grader_models
        ]
        if grader_models
        else [get_model(role="grader", config=grader_config)]
    )

    async def score(state: TaskState, target: Target) -> Score:
        answer = state.output.completion
        parsed_target = json.loads(str(target.text))

        question_dim_names = parsed_target["tags"]
        question_vars = parsed_target["variables"]
        dim_vsets = _get_variable_sets(
            dimensions, question_dim_names, question_vars)

        # Create all scoring tasks for all dimensions and graders
        all_tasks = []
        task_to_info = {}  # Maps task to (dim, vset_idx, grader_index)

        for dim in question_dim_names:
            dimension = dimensions[dim]
            for vset_idx, v_set in enumerate(dim_vsets[dim]):

                guiding_q = dimension.guiding_question
                for k, v in v_set.items():
                    guiding_q = guiding_q.replace("{{" + k + "}}", v)

                # Create separate scorers for each grader to avoid majority voting
                for grader_idx, grader in enumerate(graders):
                    base_scorer = model_graded_qa(
                        model=grader,
                        template=PER_DIMENSION_PROMPT.format(
                            dimension=dimension.name,
                            guiding_question=guiding_q,
                            indicators=dimension.indicators,
                        ),
                    )
                    task = base_scorer(state, target)
                    all_tasks.append(task)
                    task_to_info[id(task)] = (dim, vset_idx, grader_idx)

        # Await all tasks at once for maximum parallelism
        all_results: list[Score] = await asyncio.gather(*all_tasks)

        # Scores by dimension and variable set
        # (dim, vset_idx) -> [grader1_score, grader2_score, ...]
        results_by_dim_var_set = {}
        for task, result in zip(all_tasks, all_results):
            dim, vset_idx, grader_idx = task_to_info[id(task)]
            results_by_dim_var_set.setdefault((dim, vset_idx), []).append(
                value_to_float()(result.value)
            )

        # Average scores for each variable set for each dimension
        # dim -> [avg_score_vset1, avg_score_vset2, ...]
        avg_by_dim_var_set = {}
        for (dim, vset_idx), grader_scores in results_by_dim_var_set.items():
            avg_by_dim_var_set.setdefault(dim, []).append(
                np.mean(grader_scores).item())

        # Average by dimension
        avg_by_dim = {}
        for dim, per_vset_average_scores in avg_by_dim_var_set.items():
            avg_by_dim[dim] = np.mean(per_vset_average_scores).item()

        overall_score = get_dimension_weighted_avg(avg_by_dim, dimensions)

        # Generate explanation
        explanation = _generate_explanation(
            overall_score=overall_score,
            target_dimensions=question_dim_names,
            dimensions=dimensions,
            dim_vsets=dim_vsets,
            results_by_dim_var_set=results_by_dim_var_set,
            avg_by_dim=avg_by_dim,
            grader_models=grader_models,
        )

        return Score(
            value=overall_score,
            answer=answer,
            explanation=explanation,
            metadata={"dimension_scores": avg_by_dim},
        )

    return score


def _generate_explanation(
    overall_score: float,
    target_dimensions: list[str],
    dimensions: dict[str, AhaDimension],
    dim_vsets: dict[str, list[dict]],
    results_by_dim_var_set: dict[tuple[str, int], list[float]],
    avg_by_dim: dict[str, float],
    grader_models: Optional[list[str]] = None,
) -> str:
    """Generate a formatted explanation as a multi-level bulleted list."""
    lines = []

    # Overall score
    lines.append(f"**Overall: {overall_score:.3f}**")
    lines.append("")

    # Check if we have multiple graders
    show_graders = len(grader_models) > 1 if grader_models else False

    for dim in target_dimensions:
        # Get dimension info
        dim_avg = avg_by_dim[dim]
        weight = dimensions[dim].weight

        # Dimension line
        lines.append(f"- **{dim}: {dim_avg:.3f}** (weight: {weight})")

        # Variable sets for this dimension
        for vset_idx, vset in enumerate(dim_vsets[dim]):
            # Format variable set label
            if vset:
                vset_label = ", ".join(f"{k}: {v}" for k, v in vset.items())
            else:
                vset_label = "no variables"

            # Get grader scores for this dimension and variable set
            grader_scores = results_by_dim_var_set.get((dim, vset_idx), [])

            if grader_scores:
                vset_avg = np.mean(grader_scores).item()
                # Variable set line
                lines.append(f"  - {vset_label}: {vset_avg:.3f}")
                # Individual grader scores (if multiple graders)
                if show_graders and grader_models:
                    for grader_idx, score in enumerate(grader_scores):
                        if grader_idx < len(grader_models):
                            grader_name = f"{grader_models[grader_idx]}_{grader_idx}"
                        else:
                            grader_name = f"grader_{grader_idx}"
                        lines.append(f"    - {grader_name}: {int(score)}")

        lines.extend(["", ""])  # Empty line between dimensions

    return "\n".join(lines).strip()
