"""Agent that cleans and renames DB schemas."""

import json
import logging
import re
from typing import Callable

from meadow.agent.agent import Agent, AgentRole, ExecutorAgent, LLMAgentWithExecutors
from meadow.agent.executor.reask import ReaskExecutor
from meadow.agent.schema import AgentMessage
from meadow.agent.utils import (
    generate_llm_reply,
    print_message,
)
from meadow.client.client import Client
from meadow.client.schema import LLMConfig
from meadow.database.database import Database, get_non_matching_fks
from meadow.database.serializer import serialize_as_list
from meadow.history.message_history import MessageHistory

logger = logging.getLogger(__name__)

DEFAULT_RENAME_PROMPT = """Your goal is to clean up a schema to make detecting joins and understanding the data easier for asking queries. You can rename the columns as you see fit.

The user will give you a schema and you need to determine what the join columns are and how, if at all, they should be renamed to be more intuitive. Join columns should be the same name and columns that do not join should be named differently.

Given the schema, first explain what columns join already. Then provide a renaming of the columns that makes the schema more intuitive where join keys match; i.e. tableA.key = tableB.key. If a column does not join, it should not be named to match another join key.

Output the remapping in JSON in the following format:
```json
{
  "table_name": {
    "old_column1_name": "old_or_new_column1_name",
    ...
  },
  "table_name": {
    "old_column1_name": "old_or_new_column1_name",
    ...
  },
}
```"""


def parse_rename_and_update_db(
    messages: list[AgentMessage],
    agent_name: str,
    database: Database,
    can_reask_again: bool,
) -> AgentMessage:
    """Parse the message and update the database."""
    content = messages[-1].content
    error_message: str = None
    try:
        if "```json" in content:
            json_content = re.findall(r"```json\n(.*?)\n```", content, re.DOTALL)[-1]
        else:
            json_content = content
        content: dict[str, dict[str, str]] = json.loads(json_content)
    except json.JSONDecodeError as e:
        error_message = f"The content is not a valid JSON object.\n{e}"

    for tbl, col_map in content.items():
        # Find any duplicate col map values
        if len(set(col_map.values())) != len(col_map.values()):
            # Find the exact duplicate value
            duplicate_values = set()
            for value in col_map.values():
                if value in duplicate_values:
                    error_message = (
                        f"Duplicate column name '{value}' found in table '{tbl}'."
                    )
                    break
                duplicate_values.add(value)

    if not error_message:
        for table_name, column_mapping in content.items():
            if any(k != v for k, v in column_mapping.items()):
                try:
                    database.add_base_table_column_remap(table_name, column_mapping)
                except Exception as e:
                    error_message = f"Error adding the column remapping for table {table_name}.\n{e}"
                    break

    # Make sure FKs match
    if not error_message:
        table_as_dict = {tbl.name: tbl for tbl in database.tables}
        non_match_pairs = get_non_matching_fks(table_as_dict)
        if non_match_pairs:
            error_message = (
                f"The following FKs do not match:\n{non_match_pairs}\nThis may be ineviatable and"
                " if so, do not worry about changing anything and output the same remapping. Otherwise, please rename."
            )

    if error_message:
        database.remove_base_table_remaps()
        return AgentMessage(
            role="assistant",
            content=error_message + " Please regenerate mapping and try again.",
            requires_response=True,
            sending_agent=agent_name,
        )

    return AgentMessage(
        role="assistant",
        content="The schema has been updated.",
        sending_agent=agent_name,
    )


class SchemaRenamerAgent(LLMAgentWithExecutors):
    """Agent that generates SQL queries from user questions."""

    def __init__(
        self,
        client: Client,
        llm_config: LLMConfig,
        database: Database,
        executors: list[ExecutorAgent] = None,
        system_prompt: str = DEFAULT_RENAME_PROMPT,
        overwrite_cache: bool = False,
        silent: bool = True,
        llm_callback: Callable = None,
    ):
        """Initialize the SQL generator agent."""
        self._client = client
        self._llm_config = llm_config
        # if system_prompt == DEFAULT_RENAME_PROMPT:
        #     # Response format should be a JSON object for the output
        self._llm_config = self._llm_config.model_copy()
        self._llm_config.max_tokens = 2000
        # self._llm_config.response_format = {"type": "json_object"}
        self._database = database
        self._executors = executors
        self._system_prompt = system_prompt
        self._overwrite_cache = overwrite_cache
        self._llm_callback = llm_callback
        self._silent = silent
        self._messages = MessageHistory()
        self._role = AgentRole.EXECUTOR

        if self._executors is None:
            self._executors = [
                ReaskExecutor(
                    client=None,
                    llm_config=None,
                    database=self._database,
                    execution_func=parse_rename_and_update_db,
                    llm_callback=self._llm_callback,
                )
            ]

    @property
    def name(self) -> str:
        """Get the name of the agent."""
        return "SchemaRenamer"

    @property
    def description(self) -> str:
        """Get the description of the agent."""
        return "This agent renames columns to be more standard and useful for determining the right attributes and detecting joins. Most schemas need some cleaning unless the attributes are already very clear. The input instruction should be the phrase 'clean schema'. Do not pass the output of this agent to any other agent. Generate fresh instructions for future agents in the plan."
        # return "Always use this agent first before any other agent to clean the schema. It must be used in all plans. The instruction should be <instruction>clean schema</instruction>. Do not pass the output of this agent to any other agent. Generate fresh instructions for next agents."

    @property
    def llm_client(self) -> Client:
        """The LLM client of this agent."""
        return self._client

    @property
    def database(self) -> Database:
        """The database used by the agent."""
        return self._database

    @property
    def system_message(self) -> str:
        """Get the system message."""
        return self._system_prompt

    def set_chat_role(self, role: AgentRole) -> None:
        """Set the chat role of the agent.

        Only used for agents that have executors."""
        self._role = role

    @property
    def executors(self) -> list[ExecutorAgent] | None:
        """The executor agents that should be used by this agent."""
        return self._executors

    async def send(
        self,
        message: AgentMessage,
        recipient: Agent,
    ) -> None:
        """Send a message to another agent."""
        if not message:
            logger.error("GOT EMPTY MESSAGE")
            raise ValueError("Message is empty")
        message.receiving_agent = recipient.name
        self._messages.add_message(agent=recipient, role="assistant", message=message)
        await recipient.receive(message, self)

    async def receive(
        self,
        message: AgentMessage,
        sender: Agent,
    ) -> None:
        """Receive a message from another agent."""
        if not self._silent:
            print_message(
                message,
                from_agent=sender.name,
                to_agent=self.name,
            )
        self._messages.add_message(agent=sender, role="user", message=message)

        reply = await self.generate_reply(
            messages=self._messages.get_messages(sender), sender=sender
        )
        await self.send(reply, sender)

    async def generate_reply(
        self,
        messages: list[AgentMessage],
        sender: Agent,
    ) -> AgentMessage:
        """Generate a reply when Executor agent."""
        messages[0].content = "My schema is\n" + serialize_as_list(self.database.tables)
        chat_response = await generate_llm_reply(
            client=self.llm_client,
            messages=messages,
            tools=[],
            system_message=AgentMessage(
                role="system",
                content=self.system_message,
                sending_agent=self.name,
            ),
            llm_config=self._llm_config,
            llm_callback=self._llm_callback,
            overwrite_cache=self._overwrite_cache,
        )
        content = chat_response.choices[0].message.content
        print("CLEANER")
        print(messages[-1].content)
        print("RESOPNSE")
        print(content)
        print("-----")
        return AgentMessage(
            role="assistant",
            content=content,
            sending_agent=self.name,
            requires_execution=True,
        )