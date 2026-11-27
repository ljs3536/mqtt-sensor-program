def euclidean_dist(query_samples, prototype):
    # 유클리드 거리 공식
    # 1. 쿼리 데이터(시험 문제)의 벡터 좌표에서 기준점(Prototype)의 벡터 좌표를 뺍니다.
    # 2. 음수가 나오지 않게 제곱(** 2)을 합니다.
    # 3. 모든 차원의 값을 더한 뒤(sum), 루트(sqrt)를 씌워서 두 점 사이의 '직선 거리'를 구합니다
    dists = (query_samples - prototype) ** 2
    dists = dists.sum(1).sqrt()
    return dists