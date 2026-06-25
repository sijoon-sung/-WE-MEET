# ==============================================================================
# WE-MEET: Q-Learning 비용/SLA 인지형 의사결정 에이전트 (head/q_learning.py)
# ==============================================================================

import os
import json
import random
import yaml

class QLearningAgent:
    """
    대기 태스크 큐 상태, 활성 노드 풀 상황, 잔여 가상 예산을 기반으로
    가장 비용 효율적이면서도 마감 시한(Deadline)을 지킬 수 있는 스케줄링 행동을 학습하는 에이전트.
    """
    def __init__(self, cost_model_path=None, q_table_path="q_table.json",
                 alpha=0.1, gamma=0.9, epsilon=0.15):
        self.alpha = alpha       # 학습률 (Learning Rate)
        self.gamma = gamma       # 미래 보상 할인율 (Discount Factor)
        self.epsilon = epsilon   # 탐험율 (Exploration Rate)
        self.q_table_path = q_table_path
        
        # Q-테이블 초기화: {(state_str): {action: q_value}}
        self.q_table = {}
        
        # 요금 및 자원 프로파일 로드
        self.nodes_config = {}
        if cost_model_path and os.path.exists(cost_model_path):
            try:
                with open(cost_model_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f)
                    self.nodes_config = config.get("nodes", {})
            except Exception as e:
                print(f"[Q-Learning Agent] 설정 파일 로드 실패: {e}")
        
        # 기본 요금 정보 세팅 (cost_model.yaml 로드 실패 시 대체 대비)
        if not self.nodes_config:
            self.nodes_config = {
                "on_demand": {"cost_per_hour": 1.0, "gpu_scale_factor": 1.0},
                "spot_a": {"cost_per_hour": 0.4, "gpu_scale_factor": 0.6},
                "spot_b": {"cost_per_hour": 0.2, "gpu_scale_factor": 0.3}
            }

        # 행동 정의 (Action Space)
        # 0: ASSIGN_W1 (On-demand), 1: ASSIGN_W2 (Spot-A), 2: ASSIGN_W3 (Spot-B)
        # 3: HOLD (대기열 지연 보류), 4: SCALE_OUT (Spot 추가 동적 증설)
        self.actions = [0, 1, 2, 3, 4]
        
        # Q-테이블 자동 로드
        self.load_q_table()

    def _state_to_str(self, state):
        """상태 튜플 (q_len, active_bitmap, budget_level)을 Q-Table key용 문자열로 변환합니다."""
        return f"{state[0]}_{state[1]}_{state[2]}"

    def get_q_value(self, state, action):
        """특정 상태와 행동에 매핑된 Q-값을 반환합니다."""
        state_str = self._state_to_str(state)
        if state_str not in self.q_table:
            # 상태가 처음 등록되는 경우 모든 행동의 Q-값을 0.0으로 초기화
            self.q_table[state_str] = {a: 0.0 for a in self.actions}
        return self.q_table[state_str].get(action, 0.0)

    def choose_action(self, state, available_actions=None):
        """Epsilon-Greedy 탐색 정책을 활용하여 행동을 선정합니다."""
        if available_actions is None:
            available_actions = self.actions

        # 1. 탐험 (Exploration): 설정한 탐험 확률(epsilon) 이하일 경우 랜덤 행동을 선택
        if random.random() < self.epsilon:
            return random.choice(available_actions)
        
        # 2. 이용 (Exploitation): 가장 높은 Q-값을 가진 행동을 탐색
        state_str = self._state_to_str(state)
        if state_str not in self.q_table:
            self.q_table[state_str] = {a: 0.0 for a in self.actions}
            
        q_vals = self.q_table[state_str]
        
        # 가용한 행동들 중에서 최댓값을 고릅니다.
        best_val = -float('inf')
        best_actions = []
        
        for action in available_actions:
            q_val = q_vals.get(action, 0.0)
            if q_val > best_val:
                best_val = q_val
                best_actions = [action]
            elif q_val == best_val:
                best_actions.append(action)
                
        # 최대 Q-값이 중복될 경우 랜덤하게 선정하여 편향을 배제
        return random.choice(best_actions)

    def update_q_value(self, state, action, reward, next_state, next_available_actions=None):
        """
        Q-Learning 학습 핵심 갱신 알고리즘 (Bellman Equation 적용).
        """
        if next_available_actions is None:
            next_available_actions = self.actions

        # 현재 상태의 Q-값 가져오기
        current_q = self.get_q_value(state, action)
        
        # 다음 상태에서 취할 수 있는 최적의 행동의 Q-값 예측
        max_next_q = -float('inf')
        for next_act in next_available_actions:
            next_q = self.get_q_value(next_state, next_act)
            if next_q > max_next_q:
                max_next_q = next_q
                
        if max_next_q == -float('inf'):
            max_next_q = 0.0

        # Bellman Equation 수식을 사용하여 업데이트 계산
        new_q = current_q + self.alpha * (reward + self.gamma * max_next_q - current_q)
        
        # Q-테이블 갱신
        state_str = self._state_to_str(state)
        self.q_table[state_str][action] = new_q

    def calculate_reward(self, success, execution_time, worker_type, delay_time, deadline_exceeded):
        """
        보상 함수(Reward Function) 수식 모델 구현.
        - SLA 보너스: +10.0 (성공 완료 시)
        - 시간당 요금 차감 (실행 시간에 비례)
        - 마감 시한 초과(지연) 시 초당 -5.0의 엄격한 패널티 적용
        """
        reward = 0.0
        
        # 1. SLA 완료 보너스
        if success:
            reward += 10.0
            
        # 2. 실행 비용 감점 (Cost_run = Cost_worker * Time_execution)
        # 시간당 요금 모델을 초 단위로 환산하여 감산 적용
        cost_profile = self.nodes_config.get(worker_type, {})
        cost_per_hour = cost_profile.get("cost_per_hour", 0.0)
        execution_cost = cost_per_hour * (execution_time / 3600.0)
        
        # 비용 가중치를 곱하여 보상에서 차감 (예산 절약 유도)
        reward -= 2.0 * execution_cost
        
        # 3. 지연 페널티 (SLA 마감 기한 초과 시 초당 -5.0 감점)
        if deadline_exceeded:
            reward -= 5.0 * delay_time
            
        return reward

    def save_q_table(self):
        """학습된 Q-Table 데이터를 로컬 JSON 파일로 영구 보존합니다."""
        try:
            with open(self.q_table_path, 'w', encoding='utf-8') as f:
                json.dump(self.q_table, f, indent=4)
        except Exception as e:
            print(f"[Q-Learning Agent] Q-Table 저장 오류: {e}")

    def load_q_table(self):
        """로컬 저장소로부터 기존에 학습되어 저장된 Q-Table을 불러옵니다."""
        if os.path.exists(self.q_table_path):
            try:
                with open(self.q_table_path, 'r', encoding='utf-8') as f:
                    self.q_table = json.load(f)
                print(f"[Q-Learning Agent] Q-Table 로드 성공. (보존된 상태수: {len(self.q_table)})")
            except Exception as e:
                print(f"[Q-Learning Agent] Q-Table 로드 실패: {e}")
        else:
            print("[Q-Learning Agent] 신규 Q-Table을 생성합니다.")
