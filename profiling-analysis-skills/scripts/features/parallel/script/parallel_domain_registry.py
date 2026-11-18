"""Feature-2 canonical communication identities; equal membership is not identity."""
BASE_DOMAINS = ('PP', 'DP', 'EP', 'CP', 'TP', 'GLOBAL')
EXTENDED_DOMAINS = ('TP-Extend-EP', 'DP-Modulo-EP', 'DP-With-CP', 'DP-With-CP-Modulo-EP')
DOMAINS = BASE_DOMAINS + EXTENDED_DOMAINS
STAGE_DOMAINS = ('DP', 'EP', 'CP', 'TP') + EXTENDED_DOMAINS
SEMANTIC_EXTENSIONS = {
    'tp_exp': 'TP-Extend-EP', 'tp_ep': 'TP-Extend-EP', 'tp-ep': 'TP-Extend-EP',
    'tensor_and_expert_parallel': 'TP-Extend-EP', 'tp_extend_ep': 'TP-Extend-EP',
    'dp_modulo_exp': 'DP-Modulo-EP', 'dp_modulo_ep': 'DP-Modulo-EP',
    'dp_cp': 'DP-With-CP', 'dp-cp': 'DP-With-CP', 'dp_with_cp': 'DP-With-CP',
    'dp_modulo_exp_cp': 'DP-With-CP-Modulo-EP', 'dp_modulo_ep_cp': 'DP-With-CP-Modulo-EP',
    'dp_with_cp_modulo_ep': 'DP-With-CP-Modulo-EP',
    **{name.casefold(): name for name in EXTENDED_DOMAINS},
}
