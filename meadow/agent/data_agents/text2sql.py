"""SQL Generator Agent."""

import logging
import re

import sqlglot

from meadow.agent.agent import Agent, DataAgent
from meadow.agent.schema import AgentMessage
from meadow.agent.utils import (
    generate_llm_reply,
    has_termination_condition,
    print_message,
)
from meadow.client.client import Client
from meadow.client.schema import LLMConfig
from meadow.database.database import Database
from meadow.database.serializer import serializ_as_xml
from meadow.history.message_history import MessageHistory

logger = logging.getLogger(__name__)

DEFAULT_SQL_PROMPT = """Given the table schema and user's question, generate a DuckDB SQL query that answers it. If the query involves multiple steps, describe each step in detail. Use <sql1>, <sql2>, ... tags for the SQL, depending on if previous queries were talked about in the conversation. IMPORTANT: if you want to use a prior query's result as a subquery or table, use a (SELECT * FROM sql#) subquery statement with the # is replaced with the number of the sql tag. Once the user is satisfied with the SQL, output {termination_message} tag.

{schema}
"""


def prettify_sql(sql: str) -> str:
    """Prettify the SQL query."""
    try:
        sql = sqlglot.parse_one(sql, dialect="duckdb").sql(
            dialect="duckdb", pretty=True
        )
    except Exception as e:
        logger.warning(f"Failed to parse SQL in DuckDB format. sql={sql}, e={e}")
        pass
    return sql


def replace_tag_with_table(sql: str) -> str:
    """Return SQL over view SELECT statement.

    Assumes the name in the tag is the name of the view.
    """

    sql_tags = re.findall(r"\({0,1}<sql\d+>\){0,1}", sql)
    for tag in sql_tags:
        sql = sql.replace(tag, f"(SELECT * FROM {tag[1:-1]})")
    return sql


def parse_sqls(message: str) -> dict[str, str]:
    """Extract the SQL query from the response."""
    # find <sql1>, <sql2>, ... tags and return the SQL query
    # this will return a list of tuples where the tuples are the groups
    sql_components = re.findall(r"(<sql\d+>(.*?)<\/sql\d+>)", message, re.DOTALL)
    if not sql_components:
        raise ValueError("SQL not found in the response.")
    sql_dict = {}
    for sql_pair in sql_components:
        sql_with_tag, sql = sql_pair
        match = re.search(r"<(sql\d+)>", sql_with_tag)
        if match:
            sql = sql.replace(";", "").strip()
            sql_dict[match.group(1)] = sql
    return sql_dict


class SQLGeneratorAgent(DataAgent):
    """Agent that generates SQL queries from user questions."""

    def __init__(
        self,
        client: Client,
        llm_config: LLMConfig,
        database: Database,
        system_prompt: str = DEFAULT_SQL_PROMPT,
        termination_message: str = "<exit>",
        overwriting_cache: bool = False,
        silent: bool = True,
    ):
        """Initialize the SQL generator agent."""
        self._client = client
        self._llm_config = llm_config
        self._database = database
        self._system_prompt = system_prompt
        self._termination_message = termination_message
        self._overwriting_cache = overwriting_cache
        self._silent = silent
        self._messages = MessageHistory()

    @property
    def name(self) -> str:
        """Get the name of the agent."""
        return "SQLGenerator"

    @property
    def description(self) -> str:
        """Get the description of the agent."""
        return "Generates SQL queries based on user questions."

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
        serialized_schema = serializ_as_xml(self.database.tables)
        return self._system_prompt.format(
            schema=serialized_schema, termination_message=self._termination_message
        )

    async def send(
        self,
        message: AgentMessage,
        recipient: Agent,
    ) -> None:
        """Send a message to another agent."""
        if not message:
            logger.error("GOT EMPTY MESSAGE")
            raise ValueError("Message is empty")
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
        """Generate a reply based on the received messages."""
        chat_response = await generate_llm_reply(
            client=self.llm_client,
            messages=messages,
            tools=[],
            system_message=AgentMessage(
                role="system",
                content=self.system_message,
                generating_agent=self.name,
            ),
            llm_config=self._llm_config,
            overwrite_cache=self._overwriting_cache,
        )
        content = chat_response.choices[0].message.content
        # print("SQL AGENT CONTENT", content)
        # print("*****")
        if has_termination_condition(content, self._termination_message):
            return AgentMessage(
                role="assistant",
                content=content,
                tool_calls=None,
                generating_agent=self.name,
                is_termination_message=True,
            )
        else:
            sql_dict = parse_sqls(content)
            # update history with new SQL
            largest_k = max(sql_dict.keys(), key=lambda x: int(x[3:]))
            for k, v in sql_dict.items():
                if view_table := self.database.get_table(k):
                    if v != view_table.view_sql:
                        print("BAD", k, "\n", view_table.view_sql, "*****\n", v)
                    continue
                else:
                    v = replace_tag_with_table(v)
                    v = self.database.normalize_query(v)
                    self.database.add_view(name=k, sql=v, normalize_to_base_tables=True)
            # get the last sql and return it fully parsed
            last_sql = prettify_sql(self.database.get_table(largest_k).view_sql)
            try:
                last_sql_df = self.database.run_sql_to_df(last_sql).head(5)
            except Exception as e:
                logger.warning(f"Failed to run SQL in DuckDB. sql={last_sql}, e={e}")
                last_sql_df = None
            user_content = f"SQL:\n{last_sql}"
            if last_sql_df is not None:
                user_content += f"\n\nTable:\n{last_sql_df.to_string()}"
            return AgentMessage(
                role="assistant",
                content=content,
                display_content=user_content,
                tool_calls=None,
                generating_agent=self.name,
            )
