import logging

from langgraph.graph import StateGraph, START
from langchain.prompts import PromptTemplate
from langchain_openai import ChatOpenAI
from langchain_core.output_parsers import StrOutputParser

from .agent import BaseAgent, register_agent
from .agent import AGENT_REGISTRY
from .prompts import choose_agent_prompt
from ..utils.utils import chunk_string
from ..utils.graph_state import MessageState
from ..utils.model_config import MODEL


logger = logging.getLogger(__name__)


@register_agent
class DefaultAgent(BaseAgent):

    description = "Default Agent if there is no specific agent for the user's query"

    def __init__(self):
        super().__init__()
        self.llm = ChatOpenAI(model="gpt-4o", timeout=30000)
        self.base_choice = "MessageAgent"
        self.available_agents = {name: AGENT_REGISTRY[name].description for name in AGENT_REGISTRY.keys() if name != "DefaultAgent"}

    def _choose_agent(self, state: MessageState, limit=2):
        user_message = state['user_message']
        agents_info = "\n".join([f"{name}: {description}" for name, description in self.available_agents.items()])
        agents_name = ", ".join(self.available_agents.keys())

        prompt = PromptTemplate.from_template(choose_agent_prompt)
        input_prompt = prompt.invoke({"message": user_message.message, "formatted_chat": user_message.history, "agents_info": agents_info, "agents_name": agents_name})
        chunked_prompt = chunk_string(input_prompt.text, tokenizer=MODEL["tokenizer"], max_length=MODEL["context"])
        logger.info(f"Chunked prompt for deciding default agent: {chunked_prompt}")
        final_chain = self.llm | StrOutputParser()
        while limit > 0:
            answer = final_chain.invoke(chunked_prompt)
            for agent_name in self.available_agents.keys():
                if agent_name in answer:
                    logger.info(f"Chosen agent: {agent_name}")
                    return agent_name
            limit -= 1
        logger.info(f"Base agent chosen: {self.base_choice}")
        return self.base_choice
    
    def execute(self, msg_state: MessageState):
        chose_agent = self._choose_agent(msg_state)
        agent = AGENT_REGISTRY[chose_agent]()
        result = agent.execute(msg_state)
        return result