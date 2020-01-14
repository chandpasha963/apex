import time
from copy import deepcopy
import torch
import torch.optim as optim
from torch.utils.data.sampler import BatchSampler, SubsetRandomSampler
from torch.distributions import kl_divergence
from ..utils.logging import Logger
from torch.utils.tensorboard import SummaryWriter

import numpy as np
from rl.algos import PPO
import sys

# TODO:
# env.mirror() vs env.matrix?

# TODO: use magic to make this reuse more code (callbacks etc?)

class MirrorPPO(PPO):
    def update(self, policy, old_policy, optimizer,
               observations, actions, returns, advantages,
               env_fn
    ):
        env = env_fn()
        mirror_observation = env.mirror_observation
        if env.clock_based:
            mirror_observation = env.mirror_clock_observation
        mirror_action = env.mirror_action
        sym_tuples = False
        mir_loss = True
        print("minibatch_size / 2: ", self.minibatch_size)
        if sym_tuples:
            # Use only half of minibatch_size since mirror states will double the minibatch size
            minibatch_size = int(self.minibatch_size / 2) or advantages.numel()
        else:
            minibatch_size = self.minibatch_size or advantages.numel()
        
        max_act = -np.inf

        for _ in range(self.epochs):
            losses = []
            sampler = BatchSampler(
                SubsetRandomSampler(range(advantages.numel())),
                minibatch_size,
                drop_last=True
            )
            for indices in sampler:
                indices = torch.LongTensor(indices)

                # obs_batch = observations[indices]
                orig_obs = observations[indices]
                # obs_batch = torch.cat(
                #     [obs_batch,
                #      obs_batch @ torch.Tensor(env.obs_symmetry_matrix)]
                # ).detach()

                # action_batch = actions[indices]
                orig_act = actions[indices]
                # action_batch = torch.cat(
                #     [action_batch,
                #      action_batch @ torch.Tensor(env.action_symmetry_matrix)]
                # ).detach()

                return_batch = returns[indices]
                # return_batch = torch.cat(
                #     [return_batch,
                #      return_batch]
                # ).detach()

                advantage_batch = advantages[indices]
                # advantage_batch = torch.cat(
                #     [advantage_batch,
                #      advantage_batch]
                # ).detach()

                if env.clock_based:
                    mir_obs = mirror_observation(orig_obs, env.clock_inds)
                else:
                    mir_obs = mirror_observation(orig_obs)
                if sym_tuples:
                    mir_actions = mirror_action(orig_act)
                    obs_batch = torch.cat([orig_obs, mir_obs])
                    action_batch = torch.cat([orig_act, mir_actions])
                    # obs_noise = 0.5*torch.rand(orig_obs.shape) - 0.25
                    # obs_batch = torch.cat([orig_obs, obs_noise])
                    # action_noise = 0.5*torch.rand(orig_act.shape) - 0.25
                    # action_batch = torch.cat([orig_act, action_noise])
                    return_batch = torch.cat([return_batch, return_batch])
                    advantage_batch = torch.cat([advantage_batch, advantage_batch])
                else:
                    obs_batch = orig_obs
                    action_batch = orig_act

                # print("mirror act mean: {0:.3f}\t orig act mean: {1:.3f}".format(torch.mean(torch.abs(mir_actions)), torch.mean(torch.abs(orig_act))))
                # if max_act < np.max(torch.abs(orig_act).numpy()):
                #     max_act = np.max(torch.abs(orig_act).numpy())

                values, pdf = policy.evaluate(obs_batch)

                # TODO, move this outside loop?
                with torch.no_grad():
                    _, old_pdf = old_policy.evaluate(obs_batch)
                    old_log_probs = old_pdf.log_prob(action_batch).sum(-1, keepdim=True)
                
                log_probs = pdf.log_prob(action_batch).sum(-1, keepdim=True)
                if torch.any(torch.isnan(log_probs)) or torch.max(log_probs) > 20:
                    print("log prob is large or nan, skipping batch")
                    continue
                
                ratio = (log_probs - old_log_probs).exp()

                cpi_loss = ratio * advantage_batch
                clip_loss = ratio.clamp(1.0 - self.clip, 1.0 + self.clip) * advantage_batch
                actor_loss = -torch.min(cpi_loss, clip_loss).mean()

                critic_loss = 0.5 * (return_batch - values).pow(2).mean()

                # Mirror Symmetry Loss
                # _, deterministic_actions = policy(obs_batch)
                _, deterministic_actions = policy(orig_obs)
                _, mirror_actions = policy(mir_obs)
                # if env.clock_based:
                #     mir_obs = mirror_observation(obs_batch, env.clock_inds)
                    # _, mirror_actions = policy(mir_obs)
                # else: 
                    # _, mirror_actions = policy(mirror_observation(obs_batch))
                mirror_actions = mirror_action(mirror_actions)

                mirror_loss = 5 * (deterministic_actions - mirror_actions).pow(2).mean()

                entropy_penalty = -self.entropy_coeff * pdf.entropy().mean()

                # TODO: add ability to optimize critic and actor seperately, with different learning rates

                optimizer.zero_grad()
                if mir_loss:
                    (actor_loss + critic_loss + mirror_loss + entropy_penalty).backward()
                else:
                    # print('no mirror loss')
                    (actor_loss + critic_loss + entropy_penalty).backward()

                # Clip the gradient norm to prevent "unlucky" minibatches from 
                # causing pathalogical updates
                torch.nn.utils.clip_grad_norm_(policy.parameters(), self.grad_clip)
                optimizer.step()

                losses.append([actor_loss.item(),
                                pdf.entropy().mean().item(),
                                critic_loss.item(),
                                ratio.mean().item(),
                                mirror_loss.item()])

            # TODO: add verbosity arguments to suppress this
            if not losses:
                print("no updates made")
            else:
                print(' '.join(["%g"%x for x in np.mean(losses, axis=0)]))
            grads = np.concatenate([p.grad.data.numpy().flatten() for p in policy.parameters() if p.grad is not None])
            print("grad norm: ", np.sqrt(np.mean(np.square(grads))))

            # Early stopping 
            if kl_divergence(pdf, old_pdf).mean() > 0.02:
                print("Max kl reached, stopping optimization early.")
                break
        return np.mean(losses, axis=0), max_act

    def train(self,
              env_fn,
              policy, 
              n_itr,
              logger=None):

        old_policy = deepcopy(policy)

        optimizer = optim.Adam(policy.parameters(), lr=self.lr, eps=self.eps)
        # optimizer = optim.SGD(policy.parameters(), lr=self.lr, momentum=0.99, nesterov=False)

        opt_time = np.zeros(n_itr)
        samp_time = np.zeros(n_itr)
        eval_time = np.zeros(n_itr)

        start_time = time.time()
        curr_lr = self.lr

        for itr in range(n_itr):
            print("********** Iteration {} ************".format(itr))
            print("Policy name: ", self.name)

            sample_start = time.time()
            batch = self.sample_parallel(env_fn, policy, self.num_steps, self.max_traj_len)

            print("time elapsed: {:.2f} s".format(time.time() - start_time))
            samp_time[itr] = time.time() - sample_start
            print("sample time elapsed: {:.2f} s".format(samp_time[itr]))

            observations, actions, returns, values = map(torch.Tensor, batch.get())
            advantages = returns - values
            advantages = (advantages - advantages.mean()) / (advantages.std() + self.eps)

            minibatch_size = self.minibatch_size or advantages.numel()
            print("minibatch size: ", minibatch_size)
            print("timesteps in batch: %i" % advantages.numel())
            self.total_steps += advantages.numel()

            old_policy.load_state_dict(policy.state_dict())  # WAY faster than deepcopy

            optimizer_start = time.time()

            losses, max_act = self.update(policy, old_policy, optimizer, observations, actions, returns, advantages, env_fn) 
            # Update learning rate
            # if not curr_lr <= 1e-3:
            #     curr_lr -= (self.lr - 1e-3) / 3000
            #     for param_group in optimizer.param_groups:
            #         param_group['lr'] = curr_lr
            # print("Current learning rate: ", curr_lr)
           
            opt_time[itr] = time.time() - optimizer_start
            print("optimizer time elapsed: {:.2f} s".format(opt_time[itr]))        


            if logger is not None:
                evaluate_start = time.time()
                eval_proc = min(self.n_proc, 24)
                test = self.sample_parallel(env_fn, policy, 800 // eval_proc, self.max_traj_len, deterministic=True)
                eval_time[itr] = time.time() - evaluate_start
                print("evaluate time elapsed: {:.2f} s".format(eval_time[itr]))

                avg_eval_reward = np.mean(test.ep_returns)
                avg_batch_reward = np.mean(batch.ep_returns)
                avg_ep_len = np.mean(batch.ep_lens)
                _, pdf     = policy.evaluate(observations)
                _, old_pdf = old_policy.evaluate(observations)

                entropy = pdf.entropy().mean().item()
                kl = kl_divergence(pdf, old_pdf).mean().item()

                grads = np.concatenate([p.grad.data.numpy().flatten() for p in policy.parameters() if p.grad is not None])

                if type(logger) is Logger:
                    logger.record('Return (test)',avg_eval_reward, itr, 'Return', x_var_name='Iterations', split_name='test')
                    logger.record('Return (batch)', avg_batch_reward, itr, 'Return', x_var_name='Iterations', split_name='batch')
                    logger.record('Mean Eplen', avg_ep_len, itr, 'Mean Eplen', x_var_name='Iterations', split_name='batch')

                    logger.record('Mean KL Div', kl, itr, 'Mean KL Div', x_var_name='Iterations', split_name='batch')
                    logger.record('Mean Entropy', entropy, itr, 'Mean Entropy', x_var_name='Iterations', split_name='batch')
                    logger.dump()
                elif type(logger) is SummaryWriter:

                    sys.stdout.write("-" * 37 + "\n")
                    sys.stdout.write("| %15s | %15s |" % ('Return (test)', avg_eval_reward) + "\n")
                    sys.stdout.write("| %15s | %15s |" % ('Return (batch)', avg_batch_reward) + "\n")
                    sys.stdout.write("| %15s | %15s |" % ('Mean Eplen', avg_ep_len) + "\n")
                    sys.stdout.write("| %15s | %15s |" % ('Mean KL Div', "%8.3g" % kl) + "\n")
                    sys.stdout.write("| %15s | %15s |" % ('Mean Entropy', "%8.3g" % entropy) + "\n")
                    sys.stdout.write("-" * 37 + "\n")
                    sys.stdout.flush()

                    logger.add_scalar("Data/Return (test)", avg_eval_reward, itr)
                    logger.add_scalar("Data/Return (batch)", avg_batch_reward, itr)
                    logger.add_scalar("Data/Mean Eplen", avg_ep_len, itr)

                    logger.add_scalar("Gradients Info/Grad Norm", np.sqrt(np.mean(np.square(grads))), itr)
                    logger.add_scalar("Gradients Info/Max Grad", np.max(np.abs(grads)), itr)
                    logger.add_scalar("Gradients Info/Grad Var", np.var(grads), itr)

                    logger.add_scalar("Action Info/Max action", max_act, itr)
                    # logger.add_scalar("Action Info/Max mirror action", max_acts[1], itr)

                    logger.add_scalar("Misc/Mean KL Div", kl, itr)
                    logger.add_scalar("Misc/Mean Entropy", entropy, itr)
                    logger.add_scalar("Misc/Critic Loss", losses[2], itr)
                    logger.add_scalar("Misc/Actor Loss", losses[0], itr)
                    logger.add_scalar("Misc/Mirror Loss", losses[4], itr)

                    logger.add_scalar("Misc/Sample Times", samp_time[itr], itr)
                    logger.add_scalar("Misc/Optimize Times", opt_time[itr], itr)
                    logger.add_scalar("Misc/Evaluation Times", eval_time[itr], itr)

                    for i in range(pdf.loc.shape[1]): # go thru all actions
                        logger.add_histogram("Action Dist/action_"+str(i), pdf.loc[:,i], itr)
                else:
                    print("No Logger")

            # TODO: add option for how often to save model
            if avg_eval_reward > self.highest_reward:
                self.highest_reward = avg_eval_reward
                self.save(policy)

            print("Total time: {:.2f} s".format(time.time() - start_time))
