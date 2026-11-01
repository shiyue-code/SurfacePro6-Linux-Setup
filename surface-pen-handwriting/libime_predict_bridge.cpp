#include <vector>
#include <string>
#include <sstream>
#include <cstring>

namespace libime {
class LanguageModel {
public:
    char pad[256];
    LanguageModel(const char *file);
    ~LanguageModel();
};
class Prediction {
public:
    char pad[256];
    Prediction();
    ~Prediction();
    void setLanguageModel(const LanguageModel *model);
    std::vector<std::string> predict(const std::vector<std::string> &sentence, size_t maxSize);
};
}

struct PredictorHandle {
    libime::LanguageModel lm;
    libime::Prediction pred;
    std::string last_result;
    PredictorHandle(const char *lm_path) : lm(lm_path) {
        pred.setLanguageModel(&lm);
    }
};

extern "C" {

void* predictor_new(const char *lm_path) {
    try {
        return new PredictorHandle(lm_path);
    } catch (...) {
        return nullptr;
    }
}

void predictor_free(void *handle) {
    if (handle) {
        delete static_cast<PredictorHandle*>(handle);
    }
}

const char* predictor_predict(void *handle, const char *pipe_separated_tokens, int max_results) {
    if (!handle || !pipe_separated_tokens) return "";
    try {
        auto *h = static_cast<PredictorHandle*>(handle);
        std::vector<std::string> tokens;
        std::stringstream ss(pipe_separated_tokens);
        std::string item;
        while (std::getline(ss, item, '|')) {
            if (!item.empty()) {
                tokens.push_back(item);
            }
        }
        if (tokens.empty()) return "";
        auto res = h->pred.predict(tokens, static_cast<size_t>(max_results));
        std::ostringstream out;
        for (size_t i = 0; i < res.size(); ++i) {
            if (i > 0) out << "\t";
            out << res[i];
        }
        h->last_result = out.str();
        return h->last_result.c_str();
    } catch (...) {
        return "";
    }
}

}
