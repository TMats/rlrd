from collections import deque
from copy import deepcopy, copy
from dataclasses import dataclass, InitVar
from functools import lru_cache, reduce
from itertools import chain
import numpy as np
import torch
from torch.nn.functional import mse_loss

from agents.memory import Memory
from agents.nn import PopArt, no_grad, copy_shared, exponential_moving_average, hd_conv
from agents.util import cached_property, partial
import agents.sac_models


@dataclass(eq=0)
class Agent:
  observation_space: InitVar
  action_space: InitVar

  Model: type = agents.sac_models.Mlp
  OutputNorm: type = PopArt
  batchsize: int = 256  # training batch size
  memory_size: int = 1000000  # replay memory size
  lr: float = 0.0003  # learning rate
  discount: float = 0.99  # reward discount factor
  target_update: float = 0.005  # parameter for exponential moving average
  reward_scale: float = 500.  # multiplied by 100 compared to the since we use expected instead of cumulative reward
  entropy_scale: float = 100.  # multiplied by 100 compared to the since we use expected instead of cumulative reward
  start_training: int = 10000
  device: str = None
  training_steps: float = 1.  # training steps per environment interaction step

  model_nograd = cached_property(lambda self: no_grad(copy_shared(self.model)))

  total_updates = 0  # will be (len(self.memory)-start_training) * training_steps / training_interval

  def __post_init__(self, observation_space, action_space):
    device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
    model = self.Model(observation_space, action_space)
    self.model = model.to(device)
    self.model_target = no_grad(deepcopy(self.model))

    self.actor_optimizer = torch.optim.Adam(self.model.actor.parameters(), lr=self.lr)
    self.critic_optimizer = torch.optim.Adam(self.model.critics.parameters(), lr=self.lr)
    self.memory = Memory(self.memory_size, self.batchsize, device)

    self.outputnorm = self.OutputNorm(self.model.critic_output_layers)
    self.outputnorm_target = self.OutputNorm(self.model_target.critic_output_layers)

  def act(self, state, obs, r, done, info, train=False):
    stats = []
    state = self.model.reset() if state is None else state  # initialize state if necessary
    action, next_state, _ = self.model.act(state, obs, r, done, info, train)

    if train:
      self.memory.append(np.float32(r), np.float32(done), info, obs, action)
      total_updates_target = (len(self.memory) - self.start_training) * self.training_steps
      for self.total_updates in range(self.total_updates+1, int(total_updates_target)+1):
        if self.total_updates == 1:
          print("starting training")
        stats += self.train(),
    return action, next_state, stats

  def train(self):
    obs, actions, rewards, next_obs, terminals = self.memory.sample()  # sample a transition from the replay buffer
    new_action_distribution = self.model.actor(obs)  # outputs distribution object
    new_actions = new_action_distribution.rsample()  # samples using the reparametrization trick

    # critic loss
    next_action_distribution = self.model_nograd.actor(next_obs)  # outputs distribution object
    next_actions = next_action_distribution.sample()  # samples
    next_value = [c(next_obs, next_actions) for c in self.model_target.critics]
    next_value = reduce(torch.min, next_value)  # minimum action-value
    next_value = self.outputnorm_target.unnormalize(next_value)  # PopArt (not present in the original paper)

    # predict entropy rewards in a separate dimension from the normal rewards (not present in the original paper)
    next_action_entropy = - (1. - terminals) * self.discount * next_action_distribution.log_prob(next_actions)
    reward_components = torch.cat((
      self.reward_scale * rewards[:, None],
      self.entropy_scale * next_action_entropy[:, None],
    ), dim=1)  # shape = (batchsize, reward_components)

    # Instead of estimating the discounted cumulative future reward we're estimating the discounted reward. The expected discounted returns are proportional to cumulative returns but their scale is independent of the discount factor. This is not present in the original paper.
    value_target = (1-self.discount) * reward_components + (1. - terminals[:, None]) * self.discount * next_value
    normalized_value_target = self.outputnorm.update(value_target)  # PopArt update and normalize

    values = [c(obs, actions) for c in self.model.critics]
    assert values[0].shape == normalized_value_target.shape and not normalized_value_target.requires_grad
    loss_critic = sum(mse_loss(v, normalized_value_target) for v in values)

    # actor loss
    new_value = [c(obs, new_actions) for c in self.model.critics]  # new_actions with reparametrization trick
    new_value = reduce(torch.min, new_value)  # minimum action_values
    assert new_value.shape == (self.batchsize, 2)
    new_action_entropy = (1-self.discount) * self.entropy_scale * new_action_distribution.log_prob(new_actions)
    new_action_entropy = self.outputnorm.normalize(new_action_entropy[:, None])[:, -1]  # use only the entropy component
    loss_actor = new_action_entropy.mean() - new_value.mean()

    # update actor and critic
    self.critic_optimizer.zero_grad()
    loss_critic.backward()
    self.critic_optimizer.step()

    self.actor_optimizer.zero_grad()
    loss_actor.backward()
    self.actor_optimizer.step()

    # update target critics and normalizers
    exponential_moving_average(self.model_target.critics.parameters(), self.model.critics.parameters(), self.target_update)
    exponential_moving_average(self.outputnorm_target.parameters(), self.outputnorm.parameters(), self.target_update)

    return dict(
      loss_actor=loss_actor.detach(),
      loss_critic=loss_critic.detach(),
      outputnorm_mean=float(self.outputnorm.mean.mean()),
      outputnorm_std=float(self.outputnorm.std.mean()),
      memory_size=len(self.memory),
    )


AvenueAgent = partial(
  Agent,
  entropy_scale=0.05,
  lr=0.0002,
  memory_size=500000,
  batchsize=100,
  training_steps=1/4,
  start_training=10000,
  Model=partial(agents.sac_models.ConvModel)
)


# === tests ============================================================================================================
def test_agent():
  from agents import Training, run
  Sac_Test = partial(
    Training,
    epochs=3,
    rounds=5,
    steps=100,
    Agent=partial(Agent, memory_size=1000000, start_training=256, batchsize=4),
    Env=partial(id="Pendulum-v0", real_time=0),
  )
  run(Sac_Test)


def test_agent_avenue():
  from agents import Training, run
  from agents.envs import AvenueEnv
  Sac_Avenue_Test = partial(
    Training,
    epochs=3,
    rounds=5,
    steps=300,
    Agent=partial(AvenueAgent, device='cpu', training_interval=4, start_training=400),
    Env=partial(AvenueEnv, real_time=0),
    Test=partial(number=0),  # laptop can't handle more than that
  )
  run(Sac_Avenue_Test)


def test_agent_avenue_hd():
  from agents import Training, run
  from agents.envs import AvenueEnv
  Sac_Avenue_Test = partial(
    Training,
    epochs=3,
    rounds=5,
    steps=300,
    Agent=partial(AvenueAgent, device='cpu', training_interval=4, start_training=400, Model=partial(Conv=hd_conv)),
    Env=partial(AvenueEnv, real_time=0, width=368, height=368),
    Test=partial(number=0),  # laptop can't handle more than that
  )
  run(Sac_Avenue_Test)


if __name__ == "__main__":
  test_agent()
  # test_agent_avenue()
  # test_agent_avenue_hd()
